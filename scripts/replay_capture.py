#!/usr/bin/env python3
"""Validate and export a PyTap capture without installing Home Assistant.

python scripts/replay_capture.py /path/to/session --output /path/to/new-export
Incomplete evidence requires --allow-incomplete; source files are never changed.
"""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "pytap"))

from pytap.core.capture import iter_capture, read_manifest  # noqa: E402
from pytap.core.replay import FrameScanner, replay_events  # noqa: E402
from pytap.core.state import PersistentState  # noqa: E402


def private_output(path: Path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New directory (must not already exist)")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    try:
        manifest = read_manifest(args.capture, allow_incomplete=args.allow_incomplete)
        # Validate before creating any export files, using constant memory.
        for _ in iter_capture(args.capture, allow_incomplete=args.allow_incomplete):
            pass
        args.output.mkdir(mode=0o700)
        counts: dict[str, int] = {}
        live_drops: dict[str, int] = {}
        unknown_verdicts: dict[str, int] = {}
        metadata = manifest.get("metadata", {})
        scanner = FrameScanner(started_mid_connection=metadata.get("started_mid_connection") is True)
        with (
            private_output(args.output / "chunks.jsonl") as indexes,
            private_output(args.output / "frames.jsonl") as frames,
        ):

            def write_frame(frame):
                counts[frame["kind"]] = counts.get(frame["kind"], 0) + 1
                if frame.get("live_parser_would_drop"):
                    reason = frame["live_parser_drop_reason"]
                    live_drops[reason] = live_drops.get(reason, 0) + 1
                elif frame["kind"] == "frame" and frame.get("live_parser_would_drop") is None:
                    reason = frame["live_parser_drop_reason"]
                    unknown_verdicts[reason] = unknown_verdicts.get(reason, 0) + 1
                frames.write(json.dumps(frame, separators=(",", ":")) + "\n")

            for chunk in iter_capture(args.capture, allow_incomplete=args.allow_incomplete):
                indexes.write(
                    json.dumps(
                        {
                            "utc_ns": chunk.utc_ns,
                            "monotonic_ns": chunk.monotonic_ns,
                            "stream_offset": chunk.stream_offset,
                            "length": len(chunk.data),
                            "declared_length": chunk.declared_length,
                            "connection_id": chunk.connection_id,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                for frame in scanner.feed(chunk):
                    write_frame(frame)
            for frame in scanner.finish():
                write_frame(frame)
        state = PersistentState.from_dict(metadata.get("parser_state", {}))
        with private_output(args.output / "events.jsonl") as events:
            for event in replay_events(
                iter_capture(args.capture, allow_incomplete=args.allow_incomplete), state
            ):
                events.write(json.dumps(event, separators=(",", ":")) + "\n")
        with private_output(args.output / "summary.json") as summary:
            json.dump(
                {
                    "capture_id": manifest["capture_id"],
                    "complete": manifest["complete"],
                    "integrity_verified": bool(manifest["complete"]) and manifest["format_version"] >= 2,
                    "format_version": manifest["format_version"],
                    "stop_reason": manifest["stop_reason"],
                    "dropped_bytes": manifest["dropped_bytes"],
                    "dropped_records": manifest["dropped_records"],
                    "records": counts,
                    "frames_live_parser_would_drop": live_drops,
                    "frames_live_parser_verdict_unknown": unknown_verdicts,
                },
                summary,
                indent=2,
            )
        print(f"Exported to {args.output}; complete={manifest['complete']}; {counts}")
        return 0
    except (OSError, ValueError, KeyError) as err:
        print(f"Capture export failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
