"""Conservative auto-promotion boundary; ambiguous evidence stays in the report."""
import re

INTENT = re.compile(r"(?i)\b(?:remember|always)\b|\bmy (?:preference|preferred|default)\b|记住|长期|偏好|默认|以后.*(?:请|用)")
CONTEXT_ONLY = re.compile(
    r"(?i)\b(?:translate|quote|quoted|example|fiction|hypothetical)\b|"
    r"\b(?:do not|don't|never) (?:save|store|remember)\b|"
    r"翻译|引用|示例|例子|虚构|假设|(?:不要|别|无需)(?:保存|记住|记录)|"
    r"```|(?:^|\n)\s*>|[“”「」『』]")
POLICY_OVERRIDE = re.compile(r"(?i)ignore.{0,60}(?:instruction|rule)|system prompt|bypass.{0,40}(?:approval|permission)|忽略.{0,30}(?:规则|指令)|绕过.{0,30}(?:权限|审批)")

def statements(text):
    """Only complete explicit owner statements, never arbitrary extracted substrings.

    These conservative syntax gates do not claim universal semantic safety. Model
    judgment still selects durable facts; quoted/third-party/ambiguous input is
    review-only. Explicit file edits remain available to the owner.
    """
    if not INTENT.search(text) or CONTEXT_ONLY.search(text) or POLICY_OVERRIDE.search(text): return []
    lines = [re.sub(r"^[-*]\s+", "", line.strip()) for line in text.splitlines() if line.strip()]
    # Reject mixed instructions and quoted material before splitting any evidence.
    direct = re.compile(r"(?i)^(?:remember\b|always\b|my (?:preference|preferred|default)\b|(?:请)?记住|我的(?:长期|偏好|默认)|以后(?:请|用))")
    if not lines or any(not direct.match(line) for line in lines): return []
    result = []
    for line in lines:
        line = re.sub(r"^(?:[-*]\s+)?(?:(?:Remember|请记住|记住)\s*[:：]\s*)?", "", line.strip(), flags=re.I)
        if line and len(line) <= 1500 and '"' not in line and not line.startswith("'"):
            result.append(line)
    return result
