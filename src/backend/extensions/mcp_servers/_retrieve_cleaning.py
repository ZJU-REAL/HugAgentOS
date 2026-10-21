from typing import List, Dict, Any
import re

def clean_retrieve_document(records: List[Dict]) -> List[Dict]:
    """
    清洗知识库检索结果,只保留文件名称和内容
    
    Args:
        records (List[Dict]): 知识库检索返回的原始记录列表
        
    Returns:
        List[Dict]: 清洗后的结果,每个dict包含文件名称和文件内容
    """
    cleaned_data = []
    
    for record in records:
        segment = record.get("segment", {})
        document = segment.get("document", {})

        # 提取segment中的content
        content = segment.get("content", "")

        # 提取document中的name和id
        file_name = document.get("name", "")
        document_id = document.get("id", "")

        token = segment.get("tokens", 0)

        item: Dict[str, Any] = {
            "文件名称": file_name,
            "文件内容": content,
            "token": token,
        }
        if document_id:
            item["document_id"] = document_id
        cleaned_data.append(item)
    
    return cleaned_data




def truncate_records_by_tokens(
    records: List[Dict[str, Any]], 
    token_threshold: int = 50000,  # token数阈值
    writer=None
) -> List[Dict[str, Any]]:
    """
    根据token数阈值截断检索结果。
    直接使用records中每条记录的tokens字段进行精确计算。
    
    Args:
        records: 检索结果列表，每条记录包含segment.tokens字段
        token_threshold: token数阈值
        writer: 流式输出写入器（可选）
    
    Returns:
        截断后的记录列表
    """
    if not records:
        return records
    
    # 计算每条记录的token数
    record_tokens = []
    for record in records:
        tokens = record.get('tokens', 0)
        record_tokens.append(tokens)
    
    total_tokens = sum(record_tokens)
    
    if total_tokens <= token_threshold:
        if writer:
            writer(f"✓ 检索到 {len(records)} 条结果，总token数: {total_tokens}，未超过阈值 {token_threshold}\n")
        return records
    
    # 从后向前裁剪
    if writer:
        writer(f"⚠ 检索结果总token数 {total_tokens} 超过阈值 {token_threshold}，开始裁剪...\n")
    
    truncated_records = records.copy()
    current_tokens = total_tokens
    removed_count = 0
    
    while current_tokens > token_threshold and len(truncated_records) > 0:
        removed_tokens = record_tokens[len(truncated_records) - 1]
        truncated_records.pop()
        current_tokens -= removed_tokens
        removed_count += 1
    
    if writer:
        writer(f"✓ 已裁剪 {removed_count} 条结果，保留 {len(truncated_records)} 条，当前token数: {current_tokens}\n")
    
    return truncated_records


def clean_html(raw_html: str) -> str:
    """
    去除文本中的 HTML 标签 (e.g. <span style...>)
    """
    if not raw_html:
        return ""
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return cleantext