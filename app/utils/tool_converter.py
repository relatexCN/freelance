"""
Tool Call Converter: XML (Claude/DeepSeek style) -> OpenAI-compatible tool_calls.

Handles:
- Standard <function_calls><invoke>...</invoke></function_calls> format
- Nested XML in parameter values
- Multi-line parameter values
- Multiple invocations in a single block
- Mixed content (text before/after XML)
"""

import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("freelance.tool_converter")

# Quick detection pattern — lightweight check before full parsing
_XML_TOOL_CALL_PATTERN = re.compile(r'<function_calls\s*>', re.DOTALL)
_XML_BLOCK_PATTERN = re.compile(
    r'(<function_calls\s*>.*?</function_calls>)',
    re.DOTALL
)


def has_xml_tool_call(content: str) -> bool:
    """Lightweight detection check for XML tool calls in content."""
    if not content:
        return False
    return bool(_XML_TOOL_CALL_PATTERN.search(content))


def extract_xml_block(content: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract the XML function_calls block from mixed content.
    
    Returns:
        (text_before, xml_block, text_after)
        If no XML block found, returns (None, None, None)
    """
    if not content:
        return None, None, None
    
    match = _XML_BLOCK_PATTERN.search(content)
    if not match:
        return None, None, None
    
    start, end = match.span()
    text_before = content[:start].strip() or None
    xml_block = match.group(1)
    text_after = content[end:].strip() or None
    
    return text_before, xml_block, text_after


def xml_to_tool_calls(content: str) -> List[Dict[str, Any]]:
    """
    Converts Claude/DeepSeek style XML function calls to OpenAI-compatible tool_calls.
    
    Supported format:
        <function_calls>
        <invoke name="read">
        <parameter name="path" string="true">/some/path</parameter>
        <parameter name="code">def foo():
            pass</parameter>
        </invoke>
        </function_calls>
    
    Returns:
        List of OpenAI tool_call dicts, or empty list if parsing fails.
    """
    if not content:
        return []
    
    # Extract the XML block
    _, xml_block, _ = extract_xml_block(content)
    if not xml_block:
        return []
    
    tool_calls = []
    
    try:
        # Wrap in a root element for valid XML parsing
        # Also escape any stray & characters that aren't part of XML entities
        sanitized = xml_block.replace("&", "&amp;").replace("&amp;amp;", "&amp;")
        root = ET.fromstring(sanitized)
        
        for invoke_elem in root.findall("invoke"):
            func_name = invoke_elem.get("name", "")
            if not func_name:
                continue
            
            # Extract all parameters
            args = {}
            for param_elem in invoke_elem.findall("parameter"):
                param_name = param_elem.get("name", "")
                if not param_name:
                    continue
                # .text handles multi-line content correctly
                param_value = (param_elem.text or "").strip()
                args[param_name] = param_value
            
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": json.dumps(args, ensure_ascii=False)
                }
            })
        
        if tool_calls:
            logger.info(f"Parsed {len(tool_calls)} tool call(s) from XML")
            
    except ET.ParseError as e:
        logger.warning(f"XML parsing failed, falling back to regex: {e}")
        # Fallback to regex for malformed XML
        tool_calls = _regex_fallback_parse(xml_block)
    
    return tool_calls


def _regex_fallback_parse(xml_block: str) -> List[Dict[str, Any]]:
    """Regex-based fallback parser for malformed XML."""
    tool_calls = []
    
    invoke_pattern = re.compile(r'<invoke\s+name="(.*?)">(.*?)</invoke>', re.DOTALL)
    param_pattern = re.compile(r'<parameter\s+name="(.*?)"[^>]*>(.*?)</parameter>', re.DOTALL)
    
    for name, params_xml in invoke_pattern.findall(xml_block):
        args = {}
        for p_name, p_value in param_pattern.findall(params_xml):
            args[p_name] = p_value.strip()
        
        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False)
            }
        })
    
    if tool_calls:
        logger.info(f"Regex fallback parsed {len(tool_calls)} tool call(s)")
    
    return tool_calls
