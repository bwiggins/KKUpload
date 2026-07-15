from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scanner import ScannedFile


@dataclass(frozen=True)
class ListRecord:
    id: str
    name: str
    parent_id: str | None
    type: str = "manual"


@dataclass(frozen=True)
class RequiredList:
    path: tuple[str, ...]
    name: str
    parent_path: tuple[str, ...]


@dataclass(frozen=True)
class PlannedFile:
    file_path: Path
    relative_path: Path
    destination_path: tuple[str, ...] | None


@dataclass(frozen=True)
class UploadPlan:
    required_lists: tuple[RequiredList, ...]
    planned_files: tuple[PlannedFile, ...]


class ListIndex:
    def __init__(self, records: tuple[ListRecord, ...]) -> None:
        self._records_by_key = {
            (record.parent_id, record.name): record
            for record in records
            if record.type == "manual"
        }
        self._path_cache: dict[tuple[str, ...], ListRecord] = {}

    def find_child(self, parent_id: str | None, name: str) -> ListRecord | None:
        return self._records_by_key.get((parent_id, name))

    def find_path(self, path: tuple[str, ...]) -> ListRecord | None:
        if not path:
            return None

        if path in self._path_cache:
            return self._path_cache[path]

        parent_id: str | None = None
        found: ListRecord | None = None

        for part in path:
            found = self.find_child(parent_id, part)
            if found is None:
                return None
            parent_id = found.id

        self._path_cache[path] = found
        return found

    def add(self, record: ListRecord) -> None:
        self._records_by_key[(record.parent_id, record.name)] = record
        self._path_cache.clear()


def build_upload_plan(
    files: tuple[ScannedFile, ...],
    *,
    import_to_root: bool,
    root_list: str,
    top_folder_name: str | None = None,
    omit_top_folder_list: bool = False,
) -> UploadPlan:
    required_paths: set[tuple[str, ...]] = set()
    planned_files: list[PlannedFile] = []

    if not import_to_root:
        root_path = parse_list_path(root_list)
        required_paths.add(root_path)
    else:
        root_path = ()

    top_folder_path = (
        ()
        if omit_top_folder_list or not top_folder_name
        else (top_folder_name,)
    )

    for file in files:
        destination_path = root_path + top_folder_path + file.folder_parts

        if destination_path:
            for depth in range(1, len(destination_path) + 1):
                required_paths.add(destination_path[:depth])
            final_destination: tuple[str, ...] | None = destination_path
        else:
            final_destination = None

        planned_files.append(
            PlannedFile(
                file_path=file.path,
                relative_path=file.relative_path,
                destination_path=final_destination,
            )
        )

    required_lists = tuple(
        RequiredList(
            path=path,
            name=path[-1],
            parent_path=path[:-1],
        )
        for path in sorted(required_paths, key=lambda item: (len(item), item))
    )

    return UploadPlan(
        required_lists=required_lists,
        planned_files=tuple(planned_files),
    )


def format_list_path(path: tuple[str, ...] | None) -> str:
    if not path:
        return "(Karakeep root / no list)"
    return " / ".join(path)


def parse_list_path(value: str) -> tuple[str, ...]:
    parts = tuple(part.strip() for part in value.split("/"))
    if not parts or any(not part for part in parts):
        raise ValueError(
            "Import to list cannot contain empty path parts. "
            "Use / only between list names."
        )
    return parts
