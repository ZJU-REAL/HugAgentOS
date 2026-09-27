import { Skeleton } from 'antd';
import { LeftOutlined } from '@ant-design/icons';

export function AgentListSkeleton() {
  return (
    <div className="jx-agentPage-grid">
      {Array.from({ length: 6 }, (_, idx) => (
        <div key={idx} className="jx-agentCard jx-agentCardSkeleton" aria-hidden="true" aria-busy="true">
          <div className="jx-agentCard-body">
            <div className="jx-agentCard-head">
              <Skeleton.Avatar active size={28} shape="circle" />
              <div className="jx-agentCardSkeletonMeta">
                <Skeleton.Input active size="small" className="jx-agentCardSkeletonTitle" />
                <Skeleton.Input active size="small" className="jx-agentCardSkeletonBadge" />
              </div>
            </div>
            <Skeleton active paragraph={{ rows: 2, width: ['92%', '74%'] }} title={false} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function AgentDetailSkeleton() {
  return (
    <div className="jx-agentPage jx-agentLibraryPage" aria-busy="true">
      <div className="jx-agentDetail-top">
        <button className="jx-agentDetail-backBtn" type="button" aria-hidden="true">
          <LeftOutlined style={{ fontSize: 14 }} />
        </button>
        <div className="jx-agentDetail-content">
          <div className="jx-agentDetail-nameRow">
            <Skeleton.Avatar active size={44} shape="square" />
            <Skeleton.Input active className="jx-agentDetailSkeletonTitle" />
            <Skeleton.Input active size="small" className="jx-agentDetailSkeletonBadge" />
          </div>
          <Skeleton.Input active size="small" className="jx-agentDetailSkeletonVersion" />
          <hr className="jx-agentDetail-divider" />
          <div className="jx-agentDetail-sections">
            {Array.from({ length: 3 }, (_, idx) => (
              <section key={idx} className="jx-agentDetail-section" aria-hidden="true">
                <div className="jx-agentDetail-sectionHead">
                  <Skeleton.Input active size="small" className="jx-agentDetailSkeletonSectionTitle" />
                </div>
                <div className="jx-agentDetail-grid">
                  <div className="jx-agentDetail-field">
                    <Skeleton active paragraph={{ rows: 2, width: ['30%', '80%'] }} title={false} />
                  </div>
                  <div className="jx-agentDetail-field">
                    <Skeleton active paragraph={{ rows: 2, width: ['34%', '68%'] }} title={false} />
                  </div>
                </div>
              </section>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
