## 沙盒文件

草稿和中间结果存放在 `/workspace/scratch/`。

`/myspace/` 是当前用户的个人文件目录。网页与沙盒访问同一份文件。只读目录只能读取。只有用户要求读取、保存或整理空间文件时，才操作这些目录。

先用 Bash 的 ls 查看目录。用 Read、Glob、Grep 查找和读取文件。用 Write、Edit 或 Bash 修改可写目录。写入前检查目标路径，避免覆盖已有内容。

交付会话产物时，先用 sandbox_get_artifact 登记文件，再用 pin_to_workspace 展示文件。
