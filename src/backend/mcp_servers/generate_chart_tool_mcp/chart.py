"""
ChartAgent：调用 LLM 生成 matplotlib 绘图代码，执行后保存为 PNG artifact。
"""
import io
import os
import re
import sys
import uuid
import json
import tempfile
import contextlib
from pathlib import Path
from typing import Dict, Any

from dotenv import load_dotenv

# 把项目根目录加入 sys.path，供 core/artifacts 等包使用
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from _common import safe_stream_writer
from pydantic import BaseModel, Field

import matplotlib
matplotlib.use('Agg')   # 非交互式后端，防止 Tkinter 报错

from agentscope.message import Msg, TextBlock

from core.config.runtime_env import get_runtime_value
from core.llm.chat_models import make_chat_model
from core.llm.message_compat import extract_text_from_chat_response
from core.artifacts.store import save_artifact_bytes

load_dotenv()

# 方正中文字体目录解析（与源码位置解耦）：
#   1. JX_FONT_DIR 环境变量覆盖
#   2. 容器内永久安装位置（所有镜像 Dockerfile 都把 resources/fonts 装到这里）
#   3. 本地 dev 回退到仓库顶层 resources/fonts
# 模板里再用 os.path.isdir 兜底，目录缺失时 matplotlib 自动退回系统 CJK 字体。
def _resolve_font_dir() -> str:
    candidates = [
        os.getenv("JX_FONT_DIR", ""),
        "/usr/share/fonts/truetype/fangzheng",
        str(Path(__file__).resolve().parents[4] / "resources" / "fonts"),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return "/usr/share/fonts/truetype/fangzheng"


_FONT_DIR = _resolve_font_dir()

# 字体头部模板：在 LLM 代码执行前注入，注册中文字体并设置 rcParams
_FONT_HEADER_TEMPLATE = """\
import matplotlib.font_manager as _jx_fm
import matplotlib as _jx_mpl
import os as _jx_os
_jx_font_dir = {font_dir!r}
_jx_added = []
for _jx_f in (_jx_os.listdir(_jx_font_dir) if _jx_os.path.isdir(_jx_font_dir) else []):
    if _jx_f.lower().endswith('.ttf'):
        try:
            _jx_fp = _jx_os.path.join(_jx_font_dir, _jx_f)
            _jx_fm.fontManager.addfont(_jx_fp)
            _jx_added.append(_jx_fm.FontProperties(fname=_jx_fp).get_name())
        except Exception:
            pass
_jx_mpl.rcParams['font.sans-serif'] = _jx_added + ['WenQuanYi Zen Hei', 'Noto Sans CJK SC', 'SimHei', 'DejaVu Sans']
_jx_mpl.rcParams['axes.unicode_minus'] = False
del _jx_fm, _jx_mpl, _jx_os, _jx_font_dir, _jx_added
"""

# 保存指令模板：在 LLM 代码末尾注入，捕获 savefig 错误并打印
_SAVE_FOOTER_TEMPLATE = """\

_jx_save_ok = False
_jx_save_err = ''
try:
    import matplotlib.pyplot as _jx_plt_sv
    _jx_plt_sv.savefig({tmp_path!r}, dpi=150, bbox_inches='tight')
    _jx_plt_sv.close('all')
    _jx_save_ok = True
except Exception as _jx_e:
    _jx_save_err = str(_jx_e)
    print(f'JX_SAVE_ERROR: {{_jx_save_err}}')
"""

_SYSTEM_PROMPT = """\
你是一个 Python 数据可视化专家。你的任务是 **仅输出** 可直接执行的 Python 代码，不要包含任何解释、注释或 Markdown 代码块标记。

【严格规则】
1. 使用 matplotlib 库。
2. 代码开头必须导入 matplotlib.pyplot：
       import matplotlib.pyplot as plt
3. 绘制完成后 **禁止** 调用以下任何函数，系统会自动保存图片：
   - plt.show()
   - plt.savefig()
   - plt.close()
   - fig.savefig()
4. **不要** 输出 ```python 或 ``` 等代码块标记，直接输出纯 Python 代码。
5. **不要** 输出任何自然语言说明，只输出代码。

【中文字体】
中文字体已由系统自动配置，无需在代码中设置 plt.rcParams['font.sans-serif']。

【数据上下文】
用户提供的数据以及绘图要求如下：
"""


class ChartAgent:
    def __init__(self):
        # DB-first lookup so admin-panel model rotations apply on next ChartAgent
        # construction without restarting the MCP container.
        self.chart_llm = make_chat_model(
            model=get_runtime_value("BASE_MODEL_NAME") or "dummy-model",
            temperature=0,          # 代码生成必须确定性输出
            max_tokens=4096,
            timeout=60,
            disable_thinking=True,  # 绘图不需要思考过程，直接输出代码
            base_url=get_runtime_value("MODEL_URL") or "",
            api_key=get_runtime_value("API_KEY") or "",
            # 工具型 LLM（单发代码生成，不进 agent 压缩循环）：context_size 不参与
            # 任何计算；mcp 容器也未必能连 Config 模型配置，显式给名义值即可。
            context_size=32768,
        )

    # ── 文本清洗 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """去除思考模型输出的 <think>...</think> 块（取最后一个 </think> 之后的内容）。"""
        # 有些模型用 <think>...</think>，有些用多个嵌套块
        # 取最后一个 </think> 之后的内容最为保险
        last_end = text.rfind("</think>")
        if last_end != -1:
            text = text[last_end + len("</think>"):]
        return text.strip()

    @staticmethod
    def _extract_code(text: str) -> str:
        """
        从 LLM 输出中提取 Python 代码。
        优先顺序：
          1. ```python ... ``` 代码块
          2. ``` ... ``` 代码块（无语言标记）
          3. 整段文本（直接返回，去除非代码行）
        """
        # 尝试 ```python ... ```（允许 \r\n 换行）
        m = re.search(r"```python[ \t]*\r?\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1)

        # 尝试 ``` ... ```（无语言标记）
        m = re.search(r"```[ \t]*\r?\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1)

        # 无代码块标记——清除残余的 backtick，直接使用
        return text.replace("```", "")

    @staticmethod
    def _remove_forbidden_calls(code: str) -> str:
        """
        删除 LLM 代码中的 plt.show() / plt.savefig() / plt.close() / fig.savefig() 调用。
        这些调用会干扰系统注入的保存逻辑。
        """
        # 匹配单行调用（括号内不含换行的版本）
        forbidden = [
            r'\bplt\.show\s*\([^)]*\)',
            r'\bplt\.savefig\s*\([^)]*\)',
            r'\bplt\.close\s*\([^)]*\)',
            r'\bfig\.savefig\s*\([^)]*\)',
            r'\bfig\.show\s*\([^)]*\)',
        ]
        for pattern in forbidden:
            code = re.sub(pattern, '', code)

        # 清理因删除调用而产生的空行（连续两行以上的空行合并成一行）
        code = re.sub(r'\n{3,}', '\n\n', code)
        return code

    def _sanitize_code(self, raw: str) -> str:
        """完整清洗流程：去思考块 → 提取代码 → 删除禁止调用。"""
        text = self._strip_thinking(raw)
        code = self._extract_code(text)
        code = self._remove_forbidden_calls(code)
        return code.strip()

    # ── 执行 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _run_isolated(full_code: str) -> tuple[bool, str]:
        """
        在单一命名空间中运行代码。
        使用 exec(code, namespace) 单字典模式，避免 exec(g, l) 双命名空间导致的
        Python 3 作用域问题：列表推导/生成器/函数内部无法访问外层 locals 中的变量
        （如 plt、n 等），从而引发 NameError。
        返回 (有无异常, 输出字符串)。
        """
        buf = io.StringIO()
        namespace: Dict[str, Any] = {}
        try:
            with contextlib.redirect_stdout(buf):
                exec(full_code, namespace)  # noqa: S102
        except Exception as exc:
            captured = buf.getvalue()
            err_line = f"{type(exc).__name__}({exc!r})"
            return True, (captured + "\n" + err_line).strip()

        captured = buf.getvalue()
        has_error = bool(captured and (
            "Error" in captured or "Traceback" in captured or "JX_SAVE_ERROR" in captured
        ))
        return has_error, captured.strip()

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def generate_chart(self, data: Any, query: str) -> Dict[str, Any]:
        """
        调用 LLM 生成绘图代码，执行后保存为 PNG artifact。
        返回 {ok, file_id, url, name, size, mime_type} 或 {ok: False, error}。
        """
        # 1. 调用 LLM 生成绘图代码
        # AgentScope 2.0：model.__call__ 取 list[Msg]（非 list[dict]）
        conversation = [
            Msg(name="system", role="system",
                content=[TextBlock(type="text", text=_SYSTEM_PROMPT)]),
            Msg(name="user", role="user",
                content=[TextBlock(type="text",
                                   text=f"绘图数据:\n{data}\n\n绘图指令:\n{query}")]),
        ]
        try:
            response = await self.chart_llm(messages=conversation)
            raw_code = extract_text_from_chat_response(response)
        except Exception as e:
            return {"ok": False, "error": f"LLM 调用失败: {e}"}

        # 2. 清洗代码
        cleaned_code = self._sanitize_code(raw_code)
        if not cleaned_code:
            return {"ok": False, "error": "LLM 未返回有效 Python 代码"}

        # 3. 创建临时输出文件（路径不暴露给 LLM）
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="jx_chart_")
        os.close(tmp_fd)

        # 4. 组装完整执行代码
        font_header = _FONT_HEADER_TEMPLATE.format(font_dir=_FONT_DIR)
        save_footer = _SAVE_FOOTER_TEMPLATE.format(tmp_path=tmp_path)
        full_code = font_header + cleaned_code + save_footer

        # 5. 隔离执行
        try:
            has_error, repl_output = self._run_isolated(full_code)

            if has_error:
                print(f"[chart REPL stderr] {repl_output}", file=sys.stderr)

            # 6. 检查是否生成了非空图片文件
            if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                with open(tmp_path, "rb") as f:
                    img_bytes = f.read()
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

                chart_name = f"chart_{uuid.uuid4().hex[:8]}.png"
                artifact = save_artifact_bytes(
                    content=img_bytes,
                    name=chart_name,
                    mime_type="image/png",
                    extension="png",
                )
                return {
                    "ok": True,
                    "file_id": artifact["file_id"],
                    "url": f"/files/{artifact['file_id']}",
                    "name": chart_name,
                    "size": artifact["size"],
                    "mime_type": "image/png",
                }
            else:
                # 图片未生成——尽量给出有用的错误信息
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                detail = repl_output if repl_output else "代码执行完成但未生成图片"
                return {"ok": False, "error": f"绘图失败：{detail}"}

        except Exception as e:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            print(f"[chart] 系统异常: {e}", file=sys.stderr)
            return {"ok": False, "error": f"系统错误: {e}"}


# ── Tool 定义 ─────────────────────────────────────────────────────────────────

# 全局单例 ChartAgent（LLM 连接复用；REPL 在 generate_chart 内部每次新建）
_chart_service = ChartAgent()


async def generate_chart_tool(data: str, query: str) -> Dict[str, Any]:
    """
    高级绘图工具：根据提供的数据和绘图要求生成数据可视化图片（柱状图、折线图、饼图等）。
    图片保存到存储后返回包含 file_id 和 url 的元数据字典，前端会自动展示图片。
    """
    # 兼容 JSON 字符串和 dict 两种入参形式
    try:
        data_obj = json.loads(data) if isinstance(data, str) else data
    except Exception:
        data_obj = data

    writer = safe_stream_writer()
    writer(f"正在绘制图像：{query}...\n")
    return await _chart_service.generate_chart(data=data_obj, query=query)


if __name__ == "__main__":
    import asyncio as _asyncio

    async def _main():
        mock_data = {
            "月份": ["1月", "2月", "3月", "4月", "5月", "6月"],
            "销售额_A部门": [120, 132, 101, 134, 90, 230],
            "销售额_B部门": [220, 182, 191, 234, 290, 330],
        }
        result = await generate_chart_tool(
            data=json.dumps(mock_data, ensure_ascii=False),
            query="折线图对比 A、B 两部门上半年销售额，标题'上半年销售对比'",
        )
        print(result)

    _asyncio.run(_main())
