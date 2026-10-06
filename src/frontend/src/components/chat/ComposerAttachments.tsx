import { useEffect, useMemo } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { EASE } from '../../utils/motionTokens';
import { useComposerDraft } from '../../stores/composerStore';
import { getApiUrl } from '../../api';
import { FileAttachmentCard } from '../file';
import { exceedsPreviewLimit, getPreviewLimitBytes } from '../../utils/filePreviewSafety';

// ── Attachment card keys ────────────────────────────────────────────────
// Assign each File object a stable auto-incrementing id as the animation key. The old
// key included the array index (idx), so deleting a middle item shifted keys of the
// following cards, making them replay the entrance animation as "new cards".
let fileKeySeq = 0;
const fileKeyMap = new WeakMap<File, number>();
function getFileKey(file: File): string {
  let id = fileKeyMap.get(file);
  if (id === undefined) {
    id = ++fileKeySeq;
    fileKeyMap.set(file, id);
  }
  return `upload-${id}`;
}

const attachCardMotion = {
  layout: true,
  initial: { opacity: 0, scale: 0.85 },
  animate: { opacity: 1, scale: 1 },
  exit: { opacity: 0, scale: 0.85, transition: { duration: 0.12, ease: EASE.exit } },
  transition: { duration: 0.18, ease: EASE.brandOut },
} as const;


export function ComposerAttachments({ currentChatId, removeFile, draftKey }: {
  currentChatId: string;
  draftKey: string;
  removeFile: (index: number) => void;
}) {
  const { uploadedFiles, uploadedArtifacts, uploadingFiles, importedSpaceFiles, removeImportedSpaceFile } = useComposerDraft(draftKey);
  const hasAttachments = uploadedFiles.length > 0 || importedSpaceFiles.length > 0;
  // Object URLs for uploaded image files — revoked when files change.
  // 超大图不给缩略图：浏览器画这张小卡片也要把整幅图解码成未压缩位图（一张
  // 一亿像素的扫描件就是几百 MB 显存/内存），几张下去标签页当场被内存打崩。
  // 阈值复用 filePreviewSafety 里的图片预览上限，别再单开一个常量。
  const uploadedImageUrls = useMemo(() => {
    const imageLimit = getPreviewLimitBytes('image');
    return uploadedFiles.map((f) => (
      f.type.startsWith('image/') && !exceedsPreviewLimit(f.size, imageLimit)
        ? URL.createObjectURL(f)
        : undefined
    ));
  }, [uploadedFiles]);
  useEffect(() => {
    return () => { uploadedImageUrls.forEach((u) => u && URL.revokeObjectURL(u)); };
  }, [uploadedImageUrls]);

  return <>
      {hasAttachments && (
        <div className="jx-inputAttachments">
          <AnimatePresence initial={false}>
            {uploadedFiles.map((file, idx) => (
              <motion.div key={getFileKey(file)} {...attachCardMotion}>
                <FileAttachmentCard
                  name={file.name}
                  loading={uploadingFiles.has(file)}
                  onClose={() => removeFile(idx)}
                  previewUrl={uploadedImageUrls[idx]}
                  artifact={uploadedArtifacts.get(file) ? {
                    file_id: uploadedArtifacts.get(file)!.file_id,
                    origin: uploadedArtifacts.get(file)!.origin,
                    url: uploadedArtifacts.get(file)!.download_url || `/files/${uploadedArtifacts.get(file)!.file_id}`,
                    name: file.name,
                    mime_type: file.type,
                    size: file.size,
                    chat_id: currentChatId,
                  } : undefined}
                />
              </motion.div>
            ))}
            {(() => {
              // The same file can be imported more than once; numbering by order of
              // file_id occurrence keeps keys unique, and no array index is mixed in
              // (deleting a middle item no longer shifts later cards and replays their animation).
              const seen = new Map<string, number>();
              return importedSpaceFiles.map((file, idx) => {
                const nth = (seen.get(file.file_id) ?? 0) + 1;
                seen.set(file.file_id, nth);
                const previewUrl = file.type === 'image'
                  ? `${getApiUrl()}${file.download_url || `/files/${file.file_id}`}`
                  : undefined;
                return (
                  <motion.div key={`space-${file.file_id}-${nth}`} {...attachCardMotion}>
                    <FileAttachmentCard
                      name={file.name}
                      onClose={() => removeImportedSpaceFile(idx)}
                      previewUrl={previewUrl}
                      artifact={{
                        file_id: file.file_id,
                        origin: file.origin,
                        url: file.download_url || `/files/${file.file_id}`,
                        name: file.name,
                        mime_type: file.mime_type,
                        chat_id: currentChatId,
                      }}
                    />
                  </motion.div>
                );
              });
            })()}
          </AnimatePresence>
        </div>
      )}
  </>;
}
