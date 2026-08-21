from pathlib import Path
from pydantic import BaseModel


PathInput = str | Path


class JsonModel(BaseModel):
    def save_json(
        self,
        output_path: PathInput,
        *,
        indent: int = 2,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        output_path.write_text(
            self.model_dump_json(indent=indent),
            encoding="utf-8",
        )

        return output_path

    @classmethod
    def load_json(cls, input_path: PathInput):
        return cls.model_validate_json(
            Path(input_path).read_text(encoding="utf-8")
        )