"""Provider-neutral events; terminal success is validated by the Runner."""
import abc
from dataclasses import dataclass


@dataclass
class AdapterResult:
    final_text: str
    provider_session_id: str | None
    usage: dict | None
    stop_reason: str | None
    is_error: bool
    terminal_reason: str | None
    exit_code: int | None = None
    error_code: str | None = None
    provider_error: dict | None = None

    @property
    def successful(self):
        return (not self.is_error and self.terminal_reason == 'completed'
                and self.error_code is None and self.exit_code == 0 and bool(self.final_text.strip())
                and bool(self.provider_session_id))


    @property
    def failure_state(self):
        return {'PERMISSION': 'WAITING_APPROVAL', 'COMMAND_DENIED': 'WAITING_APPROVAL', 'AUTH': 'WAITING_AUTH',
                'QUOTA_EXCEEDED': 'WAITING_CAPACITY'}.get(self.error_code, 'FAILED')



class BaseAdapter(abc.ABC):
    @abc.abstractmethod
    async def run(self, prompt, workspace_path, provider_session_id=None, *, profile):
        """Yield started/output_delta/approval_required and one terminal result."""
        if False:
            yield
