/** Team operations are unavailable in the community edition. */
export async function createTeamFolderBatch(_teamId: string, _parentFolderId: string | null, _paths: string[]): Promise<unknown> {
  throw new Error('Team operations are unavailable');
}
export async function uploadTeamFile(_teamId: string, _folderId: string | null, _file: File, _uploadKey?: string): Promise<never> {
  throw new Error('Team operations are unavailable');
}
