export const ONTOLOGY_LIST_DEFAULT_PAGE_SIZE = 5;

export function paginateOntologyItems<T>(items: T[], current: number, pageSize: number): T[] {
  const start = (current - 1) * pageSize;
  return items.slice(start, start + pageSize);
}
