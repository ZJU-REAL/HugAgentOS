import { t } from '../../i18n';

/**
 * 「推荐任务」示例卡片。
 *
 * 纯前端常量——后端没有推荐任务/模板的概念（`scheduled_tasks` 只存用户自己建的任务），
 * 点卡片走的是普通的创建流程：把这里的 prompt / cron / name 预填进创建弹窗，
 * 用户确认后照常 `POST /v1/automations`。所以增删示例不需要动后端。
 */
export interface AutomationPreset {
  id: string;
  /** 卡片标题，同时作为创建时的默认任务名 */
  title: string;
  /** 卡片正文，同时作为定时执行的提示词 */
  prompt: string;
  /** 5 段式 cron，交给 cronToHumanReadable 渲染成「每天 09:00」这类文案 */
  cron: string;
}

export const AUTOMATION_PRESETS: AutomationPreset[] = [
  {
    id: 'daily_briefing',
    title: t('每日行业简报'),
    prompt: t('检索过去 24 小时内本行业的重要新闻、政策与竞品动态，按「事实 / 影响 / 建议关注」三段式整理成一份不超过 800 字的简报，并标注信息来源链接。'),
    cron: '0 9 * * *',
  },
  {
    id: 'work_daily_report',
    title: t('工作日报汇总'),
    prompt: t('汇总我今天的工作内容，按「已完成事项（含量化结果）/ 进行中事项（含阻塞点）/ 明日计划」三部分输出，语气简洁、条目化，可直接粘贴到日报系统。'),
    cron: '0 20 * * 1-5',
  },
  {
    id: 'weekly_summary',
    title: t('每周工作周报'),
    prompt: t('回顾本周的会话记录与产出文件，生成一份周报：本周关键进展、遇到的问题与解决方式、下周重点计划。每部分不超过 5 条。'),
    cron: '0 17 * * 5',
  },
  {
    id: 'competitor_watch',
    title: t('竞品动态监测'),
    prompt: t('搜索我关注的竞品在最近一周的产品发布、融资、人事与公开演讲信息，逐条给出时间、来源与一句话要点，并在末尾总结值得警惕的两个趋势。'),
    cron: '0 10 * * 1',
  },
  {
    id: 'inbox_digest',
    title: t('知识库新增速览'),
    prompt: t('检查我的知识库在过去一天新增或更新的文档，逐份给出 3 句话摘要与关键结论，最后列出需要我进一步确认的问题。'),
    cron: '0 9 * * *',
  },
  {
    id: 'meeting_reminder',
    title: t('周会材料准备'),
    prompt: t('提前梳理本周周会需要的材料：本周关键数据变化、上周遗留事项的进展、需要在会上拍板的决策点，输出成一页会议提纲。'),
    cron: '0 14 * * 1',
  },
];
