import {
  Alert,
  Button,
  Empty,
  Modal,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ApiOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ForkOutlined,
  ToolOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import type { OntologyBuildFailure, OntologyBuildIssue } from '../../utils/apiError';
import { t } from '../../i18n';

const { Paragraph, Text, Title } = Typography;

const ISSUE_LABELS: Record<string, string> = {
  unknown_ontology_tags: '本体标签不存在',
  missing_required_tools: '缺少工作流所需 MCP 能力',
  forbidden_tools_bound: '绑定了禁止使用的工具',
  missing_output_contract: '缺少结构化输出约定',
  missing_tool_input_schema: '缺少工具输入定义',
  missing_ontology_parameters: '工具缺少本体要求参数',
};

interface OntologyBuildValidationModalProps {
  failure: OntologyBuildFailure | null;
  onClose: () => void;
}

function IssueList({ issues, kind }: { issues: OntologyBuildIssue[]; kind: 'error' | 'warning' }) {
  if (issues.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('没有此类问题')} />;
  }

  const isError = kind === 'error';
  const Icon = isError ? CloseCircleOutlined : WarningOutlined;
  return (
    <div className="jx-ontologyValidation-issues">
      {issues.map((issue, index) => (
        <div
          className={`jx-ontologyValidation-issue jx-ontologyValidation-issue--${kind}`}
          key={`${issue.code}-${issue.workflow_id ?? 'global'}-${index}`}
        >
          <Icon className="jx-ontologyValidation-issueIcon" />
          <div className="jx-ontologyValidation-issueBody">
            <Space size={[6, 6]} wrap>
              <Text strong>{t(ISSUE_LABELS[issue.code] ?? issue.code)}</Text>
              <Tag bordered={false}>{issue.code}</Tag>
              {issue.workflow_id && (
                <Tag color="blue" icon={<ForkOutlined />}>
                  {t('工作流')} · {issue.workflow_id}
                </Tag>
              )}
            </Space>
            <Paragraph className="jx-ontologyValidation-issueMessage">
              {issue.message}
            </Paragraph>
            {issue.details.recommended_mcp_servers.length > 0 && (
              <div className="jx-ontologyValidation-mcpSuggestions">
                <Text strong><ApiOutlined /> {t('建议绑定以下 MCP')}</Text>
                {issue.details.recommended_mcp_servers.map((server) => (
                  <div className="jx-ontologyValidation-mcpCard" key={server.server_id}>
                    <div>
                      <Text strong>{server.display_name}</Text>
                      <Text code>{server.server_id}</Text>
                    </div>
                    <Space size={[5, 5]} wrap>
                      {server.provided_tools.map((tool) => (
                        <Tooltip title={tool.name} key={tool.name}>
                          <Tag color="green">{tool.display_name}</Tag>
                        </Tooltip>
                      ))}
                    </Space>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 子智能体、技能等资产在保存时未通过本体构建校验的可执行说明。 */
export function OntologyBuildValidationModal({ failure, onClose }: OntologyBuildValidationModalProps) {
  const report = failure?.report;

  return (
    <Modal
      title={(
        <Space>
          <CloseCircleOutlined style={{ color: 'var(--color-error)' }} />
          <span>{t('未通过本体构建校验')}</span>
        </Space>
      )}
      open={Boolean(failure)}
      onCancel={onClose}
      width="min(780px, calc(100vw - 32px))"
      className="jx-ontologyValidationModal"
      footer={<Button type="primary" onClick={onClose}>{t('返回修改')}</Button>}
      destroyOnHidden
    >
      {failure && report && (
        <div className="jx-ontologyValidation">
          <Alert
            type="error"
            showIcon
            message={failure.message}
            description={t('系统没有保存当前内容。请按下方建议修改名称、简介、指令或能力绑定，然后重新保存。')}
          />

          <div className="jx-ontologyValidation-summary">
            <div className="jx-ontologyValidation-count jx-ontologyValidation-count--error">
              <span>{report.errors.length}</span>
              <Text type="secondary">{t('必须修复')}</Text>
            </div>
            <div className="jx-ontologyValidation-count jx-ontologyValidation-count--warning">
              <span>{report.warnings.length}</span>
              <Text type="secondary">{t('建议处理')}</Text>
            </div>
            <div className="jx-ontologyValidation-count">
              <span>{report.matched_workflows.length}</span>
              <Text type="secondary">{t('命中工作流')}</Text>
            </div>
            <div className="jx-ontologyValidation-count">
              <span>{report.resolved_tools.length}</span>
              <Text type="secondary">{t('已识别工具')}</Text>
            </div>
          </div>

          <section className="jx-ontologyValidation-section">
            <Title level={5}>{t('必须修复的问题')}</Title>
            <IssueList issues={report.errors} kind="error" />
          </section>

          {report.suggestions.length > 0 && (
            <section className="jx-ontologyValidation-section jx-ontologyValidation-suggestions">
              <Title level={5}><BulbOutlined /> {t('如何通过校验')}</Title>
              <ol>
                {report.suggestions.map((suggestion) => <li key={suggestion}>{suggestion}</li>)}
              </ol>
            </section>
          )}

          {report.warnings.length > 0 && (
            <section className="jx-ontologyValidation-section">
              <Title level={5}>{t('建议一并处理')}</Title>
              <IssueList issues={report.warnings} kind="warning" />
            </section>
          )}

          <section className="jx-ontologyValidation-section">
            <Title level={5}>{t('本次识别结果')}</Title>
            <div className="jx-ontologyValidation-detected">
              <div>
                <Text type="secondary"><ForkOutlined /> {t('命中的领域工作流')}</Text>
                <Space size={[6, 6]} wrap>
                  {report.matched_workflows.length > 0
                    ? report.matched_workflows.map((item) => <Tag color="blue" key={item}>{item}</Tag>)
                    : <Text type="secondary">{t('未命中')}</Text>}
                </Space>
              </div>
              <div>
                <Text type="secondary"><ToolOutlined /> {t('从能力绑定中识别到的工具')}</Text>
                <Space size={[6, 6]} wrap>
                  {report.resolved_tools.length > 0
                    ? (report.resolved_tool_details.length > 0
                        ? report.resolved_tool_details
                        : report.resolved_tools.map((name) => ({ name, display_name: name })))
                      .map((item) => (
                        <Tooltip title={item.name} key={item.name}>
                          <Tag color="green">{item.display_name}</Tag>
                        </Tooltip>
                      ))
                    : <Text type="secondary">{t('未识别到工具')}</Text>}
                </Space>
              </div>
            </div>
          </section>

          <div className="jx-ontologyValidation-copy">
            <CheckCircleOutlined />
            <Text copyable={{ text: JSON.stringify(report, null, 2) }}>{t('复制完整校验报告')}</Text>
          </div>
        </div>
      )}
    </Modal>
  );
}
