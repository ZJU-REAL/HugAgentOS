import { artifactOrigin, isArchiveMember } from '../../utils/artifactAccess';
import { ArtifactFileAction } from '../file/ArtifactFileAction';
import { FileOpenControl, type FileActionPhase } from '../file/FileOpenControl';
import type { ArtifactRef } from './DeliveredFiles';

export type DeliveryPhase = FileActionPhase;

export function DeliveredFileActions({ file, onPhase, onPath }: {
  file: ArtifactRef;
  onPhase: (phase: DeliveryPhase | undefined) => void;
  onPath: (path: string) => void;
}) {
  return artifactOrigin(file) === 'local' && !isArchiveMember(file)
    ? <FileOpenControl file={file} onPhase={onPhase} onPath={onPath} />
    : <ArtifactFileAction file={file} className="hg-delivery-download" />;
}
