from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from constants import SUPPORTED_EXTENSIONS


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    relative_path: Path
    folder_parts: tuple[str, ...]


@dataclass(frozen=True)
class ScanResult:
    scanned_folders: tuple[Path, ...]
    supported_files: tuple[ScannedFile, ...]


def is_supported_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS


def scan_upload_folder(upload_folder: Path) -> ScanResult:
    upload_folder = upload_folder.resolve()
    scanned_folders: list[Path] = []
    supported_files: list[ScannedFile] = []

    for current_folder in _walk_folders(upload_folder):
        scanned_folders.append(current_folder)

        for child in sorted(current_folder.iterdir(), key=lambda item: item.name.lower()):
            if not is_supported_file(child):
                continue

            relative_path = child.relative_to(upload_folder)
            folder_relative_path = child.parent.relative_to(upload_folder)
            folder_parts = (
                ()
                if str(folder_relative_path) == "."
                else folder_relative_path.parts
            )

            supported_files.append(
                ScannedFile(
                    path=child,
                    relative_path=relative_path,
                    folder_parts=tuple(folder_parts),
                )
            )

    return ScanResult(
        scanned_folders=tuple(scanned_folders),
        supported_files=tuple(supported_files),
    )


def validate_separate_folder_tree(paths: dict[str, Path]) -> None:
    resolved = {name: path.resolve() for name, path in paths.items()}
    items = list(resolved.items())

    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if left_path == right_path:
                raise ValueError(
                    f"{left_name} and {right_name} must be different folders."
                )

            if _is_nested(left_path, right_path):
                raise ValueError(
                    f"{left_name} cannot be inside {right_name}."
                )

            if _is_nested(right_path, left_path):
                raise ValueError(
                    f"{right_name} cannot be inside {left_name}."
                )


def _walk_folders(root: Path) -> tuple[Path, ...]:
    folders: list[Path] = []

    def visit(folder: Path) -> None:
        folders.append(folder)
        children = [
            child
            for child in folder.iterdir()
            if child.is_dir()
        ]
        for child in sorted(children, key=lambda item: item.name.lower()):
            visit(child)

    visit(root)
    return tuple(folders)


def _is_nested(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False

    return candidate != parent
