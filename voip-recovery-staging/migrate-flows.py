#!/usr/bin/env python3
"""
migrate-flows.py — one-time, idempotent migration to the multi-flow data model.

Reads the existing /opt/sampath-ai/agent-config.json and writes it as the
canonical sampath-bank.json under /var/lib/sampath-ai/flows/. Seeds two preset
example flows (real-estate.json, software-company.json) if not already present.
Writes /var/lib/sampath-ai/active-flow.json pointing at sampath-bank unless an
active pointer already exists.

Safe to re-run: existing files are not overwritten unless --force is passed.
"""
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone

LEGACY_CONFIG = Path('/opt/sampath-ai/agent-config.json')
FLOWS_DIR = Path('/var/lib/sampath-ai/flows')
ACTIVE_POINTER = Path('/var/lib/sampath-ai/active-flow.json')
SEEDS_DIR = Path(__file__).parent / 'seeds'


def now_iso():
    return datetime.now(tz=timezone.utc).isoformat(timespec='seconds')


def atomic_write_json(path: Path, data: dict, mode: int = 0o640):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.chmod(mode)
    tmp.replace(path)


def migrate_sampath_bank(force: bool) -> Path:
    """Convert the legacy single-config into flows/sampath-bank.json."""
    dest = FLOWS_DIR / 'sampath-bank.json'
    if dest.exists() and not force:
        print(f"[skip] {dest} already exists (use --force to overwrite)")
        return dest

    if not LEGACY_CONFIG.exists():
        print(f"[warn] legacy config {LEGACY_CONFIG} not found; writing a minimal sampath-bank.json")
        legacy = {}
    else:
        with LEGACY_CONFIG.open() as f:
            legacy = json.load(f)

    flow = {
        'id': 'sampath-bank',
        'name': 'Sampath Bank Agent',
        'description': 'Default voice agent — Sinhala/English customer service for Sampath Bank.',
        'is_preset': True,
        'voice': legacy.get('voice', 'Aoede'),
        'model': legacy.get('model', 'gemini-3.1-flash-live-preview'),
        'language_hint': 'si-LK',
        'greeting_trigger': legacy.get(
            'greeting_trigger',
            'The customer has just connected to the call. Please greet them now.',
        ),
        'retry_greeting_trigger': legacy.get(
            'retry_greeting_trigger',
            'The customer was just brought back to you because the manager was unavailable. Apologise warmly and offer to help instead.',
        ),
        'test_mode': bool(legacy.get('test_mode', True)),
        'test_mode_number': '0779190005',
        'escalation_timeout_sec': int(legacy.get('escalation_timeout_sec', 60)),
        'transfer_rules': [
            {
                'category': 'default',
                'manager_number': str(legacy.get('manager_number', '0779190005')),
                'description': 'Default escalation target (migrated from legacy manager_number).',
            }
        ],
        'tools_enabled': [
            'save_customer_info',
            'request_human_transfer',
            'end_call',
            'find_sampath_branch',
            'get_exchange_rates',
        ],
        'tools_config': {
            'save_customer_info': {
                'fields': ['name', 'nic', 'account_number', 'phone', 'complaint', 'language', 'preferred_branch']
            }
        },
        'system_prompt': legacy.get('system_prompt', 'You are a helpful voice assistant.'),
        'custom_instructions': legacy.get('custom_instructions', ''),
        'flow': legacy.get('flow', {
            'nodes': [
                {'id': 'start', 'type': 'start', 'position': {'x': 50, 'y': 200},
                 'data': {'label': 'Start', 'greeting_text': 'Greet caller (Sinhala/English/Tamil, time-aware).'}},
                {'id': 'intent', 'type': 'intent', 'position': {'x': 280, 'y': 200},
                 'data': {'label': 'Detect intent', 'description': 'Branch/ATM info, exchange rate, complaint, transfer.'}},
                {'id': 'branch', 'type': 'tool', 'position': {'x': 520, 'y': 100},
                 'data': {'label': 'Branch lookup', 'tool_id': 'find_sampath_branch', 'arg_template': 'query = caller-mentioned location'}},
                {'id': 'rates', 'type': 'tool', 'position': {'x': 520, 'y': 200},
                 'data': {'label': 'Exchange rates', 'tool_id': 'get_exchange_rates', 'arg_template': 'currency = optional'}},
                {'id': 'transfer', 'type': 'transfer', 'position': {'x': 520, 'y': 320},
                 'data': {'label': 'Transfer to support', 'category': 'default'}},
                {'id': 'end', 'type': 'end', 'position': {'x': 780, 'y': 200},
                 'data': {'label': 'End call', 'farewell_text': 'ස්තූතියි, සුභ දවසක්.'}}
            ],
            'edges': [
                {'id': 'e1', 'source': 'start', 'target': 'intent'},
                {'id': 'e2', 'source': 'intent', 'target': 'branch', 'label': 'branch question'},
                {'id': 'e3', 'source': 'intent', 'target': 'rates', 'label': 'rate question'},
                {'id': 'e4', 'source': 'intent', 'target': 'transfer', 'label': 'wants human'},
                {'id': 'e5', 'source': 'branch', 'target': 'end'},
                {'id': 'e6', 'source': 'rates', 'target': 'end'},
            ],
            'viewport': {'x': 0, 'y': 0, 'zoom': 1}
        }),
        'created_at': now_iso(),
        'created_by': 'migrate-flows.py',
        'updated_at': now_iso(),
        'updated_by': 'migrate-flows.py',
    }
    atomic_write_json(dest, flow)
    print(f"[ok] wrote {dest} (migrated from legacy config)")
    return dest


def seed_preset(seed_name: str, force: bool):
    src = SEEDS_DIR / seed_name
    dest = FLOWS_DIR / seed_name
    if dest.exists() and not force:
        print(f"[skip] {dest} already exists")
        return
    if not src.exists():
        print(f"[err] seed {src} missing", file=sys.stderr)
        return
    with src.open() as f:
        data = json.load(f)
    data['created_at'] = data.get('created_at') or now_iso()
    data['updated_at'] = now_iso()
    atomic_write_json(dest, data)
    print(f"[ok] seeded {dest}")


def set_active_pointer(target_id: str, force: bool):
    if ACTIVE_POINTER.exists() and not force:
        with ACTIVE_POINTER.open() as f:
            current = json.load(f)
        print(f"[skip] active pointer already set to '{current.get('active_id')}' (use --force to override)")
        return
    atomic_write_json(ACTIVE_POINTER, {
        'active_id': target_id,
        'activated_at': now_iso(),
        'activated_by': 'migrate-flows.py',
    }, mode=0o644)
    print(f"[ok] active flow set to '{target_id}'")


def main():
    force = '--force' in sys.argv
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # Best-effort ownership; ignore if not root.
        shutil.chown(FLOWS_DIR, user='asterisk', group='asterisk')
    except (LookupError, PermissionError):
        pass

    migrate_sampath_bank(force)
    seed_preset('real-estate.json', force)
    seed_preset('software-company.json', force)
    set_active_pointer('sampath-bank', force)
    print("done.")


if __name__ == '__main__':
    main()
