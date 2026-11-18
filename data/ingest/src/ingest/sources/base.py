from __future__ import annotations

from abc import ABC, abstractmethod

from ingest.schemas import DatasetIn


class DataSource(ABC):
    @abstractmethod
    def load(self) -> DatasetIn:
        raise NotImplementedError

