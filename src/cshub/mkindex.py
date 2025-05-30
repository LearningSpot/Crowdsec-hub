import argparse
import base64
import contextlib
import decimal
import hashlib
import itertools
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import override

import yaml
from tap import Tap


class Parser(Tap):
    input: Path
    output: Path

    @override
    def configure(self) -> None:
        self.add_argument("--input", default=".index.json", help="The index file to read")

        self.add_argument("--output", default=".index.json", help="The index file to write")


class HubType(str):
    pass


hubtypes: list[HubType] = [
    HubType("appsec-configs"),
    HubType("appsec-rules"),
    HubType("collections"),
    HubType("contexts"),
    HubType("parsers"),
    HubType("postoverflows"),
    HubType("scenarios"),
]


class AuthorName(str):
    pass


class ItemName(str):
    pass


class Content(str):
    pass


@dataclass
class VersionDetail:
    deprecated: bool
    digest: str


@dataclass
class Item:
    path: str
    author: AuthorName
    content: Content
    long_description: str | None
    version: str
    versions: dict[str, VersionDetail]
    labels: dict[str, str] | None = None
    stage: str | None = None
    references: list[str] | None = None

    appsec_configs: list[str] | None = None
    appsec_rules: list[str] | None = None
    collections: list[str] | None = None
    contexts: list[str] | None = None
    parsers: list[str] | None = None
    postoverflows: list[str] | None = None
    scenarios: list[str] | None = None

    def set_versions(self, prev_versions: dict):
        content_hash = hashlib.sha256(base64.b64decode(self.content)).hexdigest()

        last_version = decimal.Decimal("0.0")

        for version_number, detail in prev_versions.items():
            version_decimal = decimal.Decimal(version_number)
            last_version = max(last_version, version_decimal)
            self.versions[version_number] = VersionDetail(
                deprecated=detail.get("deprecated", False),
                digest=detail["digest"],
            )
            if content_hash == detail["digest"]:
                self.version = version_number

        if self.version == "":
            last_version += decimal.Decimal("0.1")
            self.version = str(last_version)
            self.versions[self.version] = VersionDetail(
                deprecated=False,
                digest=content_hash,
            )

    def content_as_dicts(self):
        return yaml.safe_load_all(base64.b64decode(self.content))

    def set_meta_from_content(self):
        contents = list(self.content_as_dicts())
        content = contents[0]
        # XXX: ignore multiple documents after the first one
        if "labels" in content:
            self.labels = content["labels"]
            # for sigma scenarios
            if self.labels and "classification" in self.labels and self.labels["classification"] is None:
                del self.labels["classification"]
        if "description" in content:
            self.description = content["description"]
        if "references" in content:
            self.references = content["references"]

        if "appsec-configs" in content:
            self.appsec_configs = content["appsec-configs"]
        if "appsec-rules" in content:
            self.appsec_rules = content["appsec-rules"]
        if "collections" in content:
            self.collections = content["collections"]
        if "contexts" in content:
            self.contexts = content["contexts"]
        if "parsers" in content:
            self.parsers = content["parsers"]
        if "postoverflows" in content:
            self.postoverflows = content["postoverflows"]
        if "scenarios" in content:
            self.scenarios = content["scenarios"]


class CustomEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Item):
            d = o.__dict__
            # remove None or ''
            if "long_description" in d and not d.get("long_description"):
                del d["long_description"]
            if "description" in d and not d.get("description"):
                del d["description"]
            for key in list(d):
                if key == "labels":
                    # retain None for legacy
                    continue
                # remove None from dependency lists
                if d[key] is None:
                    del d[key]
            if "appsec_configs" in d:
                d["appsec-configs"] = d.pop("appsec_configs")
            if "appsec_rules" in d:
                d["appsec-rules"] = d.pop("appsec_rules")
            return d
        if isinstance(o, VersionDetail):
            return o.__dict__
        return super().default(o)


type Index = dict[HubType, dict[str, Item]]


class IndexUpdater:
    def __init__(self, index: Index):
        self.prev_index: dict = index
        self.new_index = {}

    def parse_dir(self, root: Path):
        index: Index = {}
        for hubtype, _, author, name, item in iter_types(root):
            index.setdefault(hubtype, {})
            index[hubtype][f"{author}/{name}"] = item

        # copy previous versions from previous index
        for hubtype, items in index.items():
            for full_name, item in items.items():
                prev_versions = {}
                with contextlib.suppress(KeyError):
                    prev_versions = self.prev_index[hubtype][full_name]["versions"]

                item.set_versions(prev_versions)
                item.set_meta_from_content()

        self.new_index = index

    def index_json(self) -> str:
        return json.dumps(self.new_index, sort_keys=True, indent=2, cls=CustomEncoder)


def iter_items(
    authordir: Path,
    stage_name: str | None,
) -> Iterable[tuple[AuthorName, ItemName, Item]]:
    for p in itertools.chain(authordir.glob("*/*.yaml"), authordir.glob("*/*.yml")):
        content = Content(base64.b64encode(p.read_bytes()).decode())
        author = AuthorName(p.parent.name)

        suffix = ""
        if p.name.endswith(".yaml"):
            suffix = ".yaml"
        elif p.name.endswith(".yml"):
            suffix = ".yml"

        name = ItemName(p.name.removesuffix(suffix))

        try:
            long_description = base64.b64encode(
                p.parent.joinpath(name + ".md").read_bytes(),
            ).decode()
        except FileNotFoundError:
            long_description = None

        yield (
            author,
            name,
            Item(
                path=p.as_posix(),
                author=author,
                content=content,
                version="",
                versions={},
                long_description=long_description,
                stage=stage_name,
            ),
        )


def iter_stages(
    typedir: Path,
) -> Iterable[tuple[str | None, AuthorName, ItemName, Item]]:
    hubtype = typedir.name
    if hubtype in ["parsers", "postoverflows"]:
        for stage in typedir.iterdir():
            for author, name, item in iter_items(stage, stage.name):
                yield stage.name, author, name, item
    else:
        for author, name, item in iter_items(typedir, None):
            yield None, author, name, item


def iter_types(
    root: Path,
) -> Iterable[tuple[HubType, str | None, AuthorName, ItemName, Item]]:
    for hubtype in root.iterdir():
        if hubtype.name not in hubtypes:
            continue
        if not hubtype.is_dir():
            continue
        for stage_name, author, name, item in iter_stages(hubtype):
            yield HubType(hubtype.name), stage_name, author, name, item


def main():
    parser = Parser(description="Create an index file", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    args = parser.parse_args()
    prev_index = json.loads(args.input.read_text())
    up = IndexUpdater(prev_index)
    up.parse_dir(Path())
    new_content = up.index_json()
    with args.output.open("w") as f:
        print(f"Writing to {args.output}")
        print(new_content, file=f)
