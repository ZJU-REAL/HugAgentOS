import { Pagination } from 'antd';
import { t } from '../../i18n';

interface OntologyListPaginationProps {
  current: number;
  pageSize: number;
  total: number;
  onChange: (page: number, pageSize: number) => void;
}

export function OntologyListPagination({
  current,
  pageSize,
  total,
  onChange,
}: OntologyListPaginationProps) {
  if (total <= 0) return null;

  return (
    <div className="jx-ontologyListPagination">
      <Pagination
        current={current}
        pageSize={pageSize}
        total={total}
        pageSizeOptions={[5, 10, 20, 50]}
        responsive
        showQuickJumper={total > pageSize * 3}
        showSizeChanger
        showTotal={(count) => t('共 {total} 条', { total: count })}
        onChange={onChange}
      />
    </div>
  );
}
