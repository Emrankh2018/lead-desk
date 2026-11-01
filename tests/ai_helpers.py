from leadgen.ai.provider import Completion, ProviderError


class FakeProvider:
    """Scripted provider: returns queued dicts (or raises queued exceptions) and records prompts."""
    name, model = "fake", "fake-model"

    def __init__(self, *responses, available: bool = True):
        self.responses = list(responses)
        self.prompts: list[tuple[str, str]] = []
        self.available = available

    def status(self):
        return (True, "Fake model") if self.available else (False, "Ollama is not running")

    def complete_json(self, system, user, schema):
        self.prompts.append((system, user))
        if not self.responses:
            raise ProviderError("no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return Completion(item, 1234, self.name, self.model)
