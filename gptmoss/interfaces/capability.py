import inspect
from typing import Dict, Any, Callable, List, Type, Optional, get_type_hints
from pydantic import create_model, BaseModel, Field

class CapabilityRegistry:
    def __init__(self):
        self.capabilities: Dict[str, Type] = {}

    def register(self, cls: Type) -> Type:
        name = getattr(cls, "__capability_name__", cls.__name__.lower())
        self.capabilities[name] = cls
        return cls

    def get(self, name: str) -> Optional[Type]:
        return self.capabilities.get(name.lower())

registry = CapabilityRegistry()

def capability(cls_or_name=None, description=None, name=None):
    """
    Decorator to register a class as a Capability.
    Can be used as @capability, @capability("name") or @capability(name="name", description="...")
    """
    actual_name = name or (cls_or_name if isinstance(cls_or_name, str) else None)
    
    if cls_or_name is None or isinstance(cls_or_name, str):
        def decorator(cls):
            cls.__capability_name__ = actual_name.lower() if actual_name else cls.__name__.lower()
            cls.__capability_description__ = description or cls.__doc__ or ""
            return registry.register(cls)
        return decorator
    else:
        cls = cls_or_name
        cls.__capability_name__ = actual_name.lower() if actual_name else cls.__name__.lower()
        cls.__capability_description__ = description or cls.__doc__ or ""
        return registry.register(cls)


def action(func_or_name=None, description=None, name=None):
    """
    Decorator to register a method inside a Capability class as an Action.
    """
    actual_name = name or (func_or_name if isinstance(func_or_name, str) else None)
    
    if func_or_name is None or isinstance(func_or_name, str):
        def decorator(func):
            func.__action_is_action__ = True
            func.__action_name__ = actual_name or func.__name__
            func.__action_description__ = description or func.__doc__ or ""
            return func
        return decorator
    else:
        func = func_or_name
        func.__action_is_action__ = True
        func.__action_name__ = actual_name or func.__name__
        func.__action_description__ = description or func.__doc__ or ""
        return func



def get_actions(capability_cls: Type) -> Dict[str, Callable]:
    """Retrieve all action methods from a capability class."""
    actions = {}
    for name, method in inspect.getmembers(capability_cls, predicate=inspect.isfunction):
        if getattr(method, "__action_is_action__", False):
            action_name = getattr(method, "__action_name__", name)
            actions[action_name] = method
    return actions


def generate_action_schema(capability_name: str, action_name: str, method: Callable) -> Dict[str, Any]:
    """
    Generate JSON Schema for a single capability action using python type hints.
    """
    sig = inspect.signature(method)
    type_hints = get_type_hints(method)
    
    properties = {}
    required = []
    
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "context"):
            continue
            
        param_type = type_hints.get(param_name, str)
        # Handle Union types / Optional types simply
        # In python 3.10+ UnionType exists. For simple case, we fall back to str/any if not primitive.
        
        # Simple mapping
        type_str = "string"
        if param_type is int:
            type_str = "integer"
        elif param_type is float:
            type_str = "number"
        elif param_type is bool:
            type_str = "boolean"
        elif param_type is list or getattr(param_type, "__origin__", None) is list:
            type_str = "array"
        elif param_type is dict or getattr(param_type, "__origin__", None) is dict:
            type_str = "object"
            
        param_desc = ""
        # If there's parameter documentation, we could parse it, but for now we keep it simple.
        
        properties[param_name] = {
            "type": type_str,
            "description": param_desc
        }
        
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
            
    schema_name = f"{capability_name}__{action_name}"
    description = getattr(method, "__action_description__", method.__doc__ or "")
    
    return {
        "type": "function",
        "function": {
            "name": schema_name,
            "description": description.strip(),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }
