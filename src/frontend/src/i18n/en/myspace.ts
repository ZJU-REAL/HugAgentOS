/** 英文字典（myspace 域）：key 为中文原文，value 为英文译文。 */
export const MYSPACE_DICT: Record<string, string> = {
  // ── Tab labels ──────────────────────────────────────────────────────
  '文件资产': 'File Assets',
  '会话收藏': 'Saved Chats',
  '消息通知': 'Notifications',

  // ── Tab descriptions ────────────────────────────────────────────────
  '汇集与AI会话过程中上传或生成的各类文档与图片，可按需加入你创建的私有知识库': 'All documents and images uploaded or generated during AI conversations, ready to add to your private knowledge bases.',
  '集中管理你收藏的重要会话与自动化任务，方便快速回看与继续交流': 'Manage your bookmarked conversations and automation tasks for quick review and follow-up.',
  '查看自动化任务执行结果通知，及时了解任务完成状态': 'View automation task execution result notifications and stay up to date on task completion.',

  // ── Sub-tab / scope labels ──────────────────────────────────────────
  '文件归属': 'File Scope',
  '类型筛选': 'Filter by type',
  '个人文件夹': 'Personal Folder',
  '团队文件夹': 'Team Folder',

  // ── Folder operations ───────────────────────────────────────────────
  '重命名文件夹': 'Rename Folder',
  '新建文件夹': 'New Folder',
  '新建子文件夹': 'New Subfolder',
  '新建团队文件夹': 'New Team Folder',
  '在此文件夹下新建子夹': 'New Subfolder Here',
  '删除文件夹"{name}"？': 'Delete folder "{name}"?',
  '删除文件夹「{name}」': 'Delete folder "{name}"',
  '该文件夹及其所有子文件夹、文件都将被软删除（可在数据库中找回）。': 'This folder and all its subfolders and files will be soft-deleted (recoverable from the database).',
  '该文件夹及其子目录内共有 {n} 个文件将一并被删除。此操作会级联软删，确认继续吗？': '{n} files in this folder and its subdirectories will be deleted. This will cascade as a soft delete. Confirm?',
  '该文件夹为空，确认删除吗？': 'This folder is empty. Confirm deletion?',
  '输入文件夹名称': 'Enter folder name',
  '请输入文件夹名称': 'Please enter a folder name',
  '文件夹名称过长（≤255 字符）': 'Folder name too long (max 255 characters)',
  '文件夹名称非法': 'Invalid folder name',
  '文件夹已创建': 'Folder created',

  // ── General actions ─────────────────────────────────────────────────
  '打开': 'Open',
  '移动': 'Move',

  // ── Success / error messages ─────────────────────────────────────────
  '名称不能为空': 'Name cannot be empty',
  '已重命名': 'Renamed',
  '已删除文件夹及其下 {affected} 个文件': 'Deleted folder and {affected} file(s)',
  '文件夹已删除': 'Folder deleted',
  '已上传': 'Uploaded',
  '建文件夹失败：': 'Failed to create folder: ',
  '上传完成：成功 {ok} 个，失败 {failed} 个': 'Upload complete: {ok} succeeded, {failed} failed',
  '已上传 {n} 个文件到团队文件夹': '{n} file(s) uploaded to team folder',
  '已上传到团队文件夹': 'Uploaded to team folder',
  '已创建': 'Created',

  // ── Upload ───────────────────────────────────────────────────────────
  '上传中 {done}/{total}': 'Uploading {done}/{total}',
  '上传到团队': 'Upload to Team',
  '上传中…': 'Uploading…',

  // ── Source filter ────────────────────────────────────────────────────
  '全部来源': 'All Sources',
  '用户上传': 'User Upload',
  'AI生成': 'AI Generated',
  '搜索': 'Search',

  // ── Navigation / toolbar ─────────────────────────────────────────────
  '返回上级': 'Go Back',
  '管理成员权限': 'Manage Permissions',
  '仅可查看': 'View only',

  // ── Team workspace hints ─────────────────────────────────────────────
  '你还未加入任何团队': 'You have not joined any team yet',
  '请在左侧选择一个团队或文件夹': 'Select a team or folder on the left',
  '被邀请加入团队后，这里会显示团队共享的文件与文件夹。': 'After being invited to a team, shared files and folders will appear here.',
  '选择后即可浏览、上传或管理团队文件资产。': 'Select one to browse, upload, or manage team file assets.',

  // ── Empty states ─────────────────────────────────────────────────────
  '当前文件夹暂无文件': 'No files in this folder',
  '当前文件夹暂无内容，先新建文件夹或上传文件试试': 'This folder is empty. Try creating a folder or uploading a file.',
  '无文件': 'No file',

  // ── Pagination ───────────────────────────────────────────────────────
  '加载更多': 'Load more',
  '已加载全部内容': 'All content loaded',

  // ── Automation / favorites ───────────────────────────────────────────
  '打开自动化任务失败': 'Failed to open automation task',
  '确定将这条会话从收藏列表中移除吗？': 'Remove this conversation from your favorites?',
  '保留': 'Keep',
  '已取消收藏': 'Unfavorited',
  '取消收藏失败': 'Failed to unfavorite',
  '来自「{title}」': 'From "{title}"',
  '查看自动化记录': 'View Automation Records',
  '跳转到对话': 'Go to Chat',
  '查看记录': 'View Records',
  '查看对话': 'View Chat',

  // ── Knowledge base ───────────────────────────────────────────────────
  '请先创建至少一个私有知识库': 'Please create at least one private knowledge base first',
  '当前选择中没有可加入知识库的文件': 'No eligible files in the current selection',
  '请至少选择一个目标知识库': 'Please select at least one target knowledge base',
  '已处理 {fileCount} 个文件，{addedCount} 条加入成功，{alreadyExistsCount} 条已存在': 'Processed {fileCount} files: {addedCount} added, {alreadyExistsCount} already existed',
  '已将 {fileCount} 个文件加入 {kbCount} 个知识库，正在索引': 'Added {fileCount} file(s) to {kbCount} knowledge base(s), indexing in progress',
  '所选知识库中均已存在这些文件': 'All selected files already exist in the chosen knowledge bases',
  '加入知识库失败': 'Failed to add to knowledge base',
  '加入知识库': 'Add to Knowledge Base',
  '加入私有知识库': 'Add to Private Knowledge Base',
  '确认加入': 'Confirm',
  '选择一个或多个私有知识库，用于收录已选的 {n} 个文件': 'Select one or more private knowledge bases to add the {n} selected file(s)',
  '选择一个或多个私有知识库，用于收录文件“{name}”': 'Select one or more private knowledge bases to add file "{name}"',
  '请选择目标私有知识库': 'Select target knowledge base(s)',
  '请选择一个或多个私有知识库': 'Select one or more knowledge bases',
  '查看已加入的知识库': 'View knowledge bases',
  '{n}个知识库': '{n} KB',

  // ── Document list ────────────────────────────────────────────────────
  '大小': 'Size',
  '最近更新': 'Last Modified',
  '删除文件夹': 'Delete Folder',
  '双击打开 {name}': 'Double-click to open {name}',
  '已选 {n} 项': '{n} selected',
  '移动到文件夹': 'Move to Folder',
  '复制到文件夹': 'Copy to Folder',
  '移动到团队文件夹': 'Move to Team Folder',
  '复制到团队文件夹': 'Copy to Team Folder',
  '复制失败': 'Copy failed',
  '已复制 {n} 个文件到团队': 'Copied {n} file(s) to team',
  '已复制到团队（{f} 个文件夹、{n} 个文件）': 'Copied to team ({f} folder(s), {n} file(s))',
  '确认删除 {n} 个文件': 'Delete {n} Files',
  '确定要删除选中的 {n} 个文件吗？此操作不可撤销。': 'Delete the {n} selected file(s)? This action cannot be undone.',

  // ── ImageGrid ────────────────────────────────────────────────────────
  '移至文件夹': 'Move to Folder',
  '移至团队': 'Move to Team',

  // ── Notifications ────────────────────────────────────────────────────
  '暂无通知': 'No notifications',
  '删除通知': 'Delete Notification',
  '确定要删除这条通知吗？': 'Delete this notification?',
  '批量删除': 'Delete Selected',
  '确定要删除选中的 {n} 条通知吗？': 'Delete the {n} selected notification(s)?',
  '全部删除': 'Delete All',
  '全选': 'Select All',
  '全部标为已读': 'Mark All as Read',
  '成功': 'Success',
  '失败': 'Failed',
  '标为已读': 'Mark as Read',

  // ── TeamScopeTree ────────────────────────────────────────────────────
  '文件范围': 'File Scope',
  '共 {n} 个团队': '{n} Team(s)',

  // ── TeamFolderBreadcrumb ─────────────────────────────────────────────
  '个人文件': 'Personal Files',

  // ── MoveToTeamModal ──────────────────────────────────────────────────
  '请选择目标团队或文件夹': 'Please select a target team or folder',
  '已移动': 'Moved',
  '已移动 {n} 个文件到团队': '{n} file(s) moved to team',
  '成功 {moved} / {total}，部分失败': '{moved} / {total} succeeded, some failed',
  '移动失败': 'Failed to move',
  '移动到其他文件夹': 'Move to Another Folder',
  '当前没有可写入的团队': 'No writable teams available',

  // ── TeamPermissionsModal ─────────────────────────────────────────────
  '加载失败': 'Failed to load',
  '权限已更新': 'Permissions updated',
  '成员': 'Members',
  '文件权限': 'File Permission',
  '默认：编辑（由角色决定）': 'Default: Edit (determined by role)',

  // ── MoveToPersonalFolderModal ────────────────────────────────────────
  '我的空间（根目录）': 'My Space (Root)',
  '请选择目标文件夹': 'Please select a target folder',
  '已移动 {n} 项': '{n} item(s) moved',
  '已复制 {n} 项': '{n} item(s) copied',
  '移动 {n} 个文件到…': 'Move {n} File(s) to…',
  '复制 {n} 个文件到…': 'Copy {n} File(s) to…',
  '移动到…': 'Move to…',
  '复制到…': 'Copy to…',
  '移动到这里': 'Move Here',
  '复制到这里': 'Copy Here',
  '无可选位置': 'No available location',

  // ── MySpaceImportModal ───────────────────────────────────────────────
  '暂无文件': 'No files',
  '确认引用': 'Confirm Reference',
  '确认导入': 'Confirm Import',
  '已选 {files} 个文件 + {folders} 个文件夹': '{files} file(s) + {folders} folder(s) selected',
  '已选 {n} 个文件': '{n} file(s) selected',
  '请选择要引用的文件或文件夹': 'Select files or folders to reference',
  '请选择要导入的文件': 'Select files to import',
  '从我的空间引用': 'Reference from My Space',
  '引用整个当前文件夹（含子文件夹下全部文件）': 'Reference entire current folder (including all files in subfolders)',
  '选择来源': 'Select source',
  '搜索文件名': 'Search by filename',

  // ── FileAttachmentCard ───────────────────────────────────────────────
  'PDF 文档': 'PDF',
  'Word 文档': 'Word Document',
  'Excel 表格': 'Excel Spreadsheet',
  'PPT 幻灯片': 'PowerPoint',
  'WPS 文档': 'WPS Document',
  'CSV 表格': 'CSV',
  '文本文件': 'Text File',
  '移除文件': 'Remove file',
  '移除': 'Remove',
  '下载 {name}': 'Download {name}',

  // ── FilePreviewPane ──────────────────────────────────────────────────
  '缺少下载链接': 'Missing download URL',
  '空文件': 'Empty file',
  '工作簿为空': 'Empty workbook',
  '正在加载预览…': 'Loading preview…',
  '预览加载失败': 'Preview failed to load',
  '此格式暂不支持预览': 'Preview not supported for this format',
  '点击文件右侧的眼睛图标': 'Click the eye icon next to a file',
  '支持图片、PDF、Office 文档、Markdown、代码等多种格式预览': 'Supports images, PDF, Office documents, Markdown, code, and more',
  '下载原文件': 'Download original file',
  '松开，上传到当前团队文件夹': 'Release to upload to the current team folder',
  '松开，上传到当前文件夹': 'Release to upload to the current folder',
  '松开，上传到本项目': 'Release to upload to this project',
};
