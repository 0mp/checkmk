# Stubs for kubernetes.client.models.v1beta1_volume_error (Python 2)
#
# NOTE: This dynamically typed stub was automatically generated by stubgen.

from typing import Any, Optional

class V1beta1VolumeError:
    swagger_types: Any = ...
    attribute_map: Any = ...
    discriminator: Any = ...
    message: Any = ...
    time: Any = ...
    def __init__(self, message: Optional[Any] = ..., time: Optional[Any] = ...) -> None: ...
    @property
    def message(self): ...
    @message.setter
    def message(self, message: Any) -> None: ...
    @property
    def time(self): ...
    @time.setter
    def time(self, time: Any) -> None: ...
    def to_dict(self): ...
    def to_str(self): ...
    def __eq__(self, other: Any): ...
    def __ne__(self, other: Any): ...
