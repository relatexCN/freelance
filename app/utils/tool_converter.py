import re
import json
import uuid
from typing import List, Dict, Any, Optional

def xml_to_tool_calls(content: str) -> List[Dict[str, Any]]:
    """
    Converts Claude/DeepSeek style XML function calls to OpenAI-compatible tool_calls.
    Format:
    <function_calls>
    <invoke name="read">
    <parameter name="path" string="true">/some/path</parameter>
    </invoke>
    </function_calls>
    """
    if not content or "<function_calls>" not in content:
        return []

    tool_calls = []
    
    # Match each <invoke> block
    invoke_pattern = re.compile(r'<invoke name="(.*?)">(.*?)</invoke>', re.DOTALL)
    param_pattern = re.compile(r'<parameter name="(.*?)".*?>(.*?)</parameter>', re.DOTALL)
    
    matches = invoke_pattern.findall(content)
    
    for name, params_xml in matches:
        # Extract all parameters for this invoke
        args = {}
        param_matches = param_pattern.findall(params_xml)
        for p_name, p_value in param_matches:
            # Clean up value (remove unexpected whitespace)
            args[p_name] = p_value.strip()
            
        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False)
            }
        })
        
    return tool_calls

def has_xml_tool_call(content: str) -> bool:
    """Detection check for XML tool calls."""
    return "<function_calls>" in content if content else False
