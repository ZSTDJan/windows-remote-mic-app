"""Developer-side read-only inventory of a complete exported log ZIP.

Usage: .venv Python scripts/audit_log_bundle.py <zip> [--since ISO --until ISO]
JSON goes to stdout. Never extracts files, alters a device or sends user data.
This accounts for every member/line; it does not replace reading the full failing
run and its surrounding records. Unknown formats and missing coverage are visible.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import zipfile


STAMP = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) (\w+) ')
RUN = re.compile(r'\brun=([0-9a-f]{12,64})\b')
VERSION = re.compile(r'startup: app identity: version=([0-9A-Za-z.+-]+)')
TEXT_LOG = re.compile(r'(app|hid-helper)\.log(?:\.[1-9][0-9]*)?$')
TRACE = re.compile(r'diagnostic-trace\.jsonl(?:\.[1-9][0-9]*)?$')
ROUND_GAP_SECONDS = 30 * 60  # Reviewable heuristic, never a confirmed test boundary.


def _stamp(value, zone):
    """Normalize explicit ISO bounds to the log's local wall clock, not this PC's."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace(',', '.'))
    if parsed.tzinfo is not None:
        if zone is None:
            raise ValueError('timezone_missing_for_offset_bound')
        parsed = parsed.astimezone(zone).replace(tzinfo=None)
    return parsed.strftime('%Y-%m-%d %H:%M:%S,%f')[:23]


def _gap(first, last):
    return (datetime.fromisoformat(first.replace(',', '.')) -
            datetime.fromisoformat(last.replace(',', '.'))).total_seconds()


def _focus(result, line_index, *, since, until, version, device, zone):
    lower, upper = _stamp(since, zone), _stamp(until, zone)
    if lower and upper and lower > upper:
        raise ValueError('since_after_until')
    runs = result['runs']
    starts = sorted((dict(row, version=v) for v, rows in result['versions'].items()
                     for row in rows), key=lambda row: row['time'])
    warnings = []
    eligible = lambda run: ((not version or run['version'] == version) and
                            (not device or run['device_ref'] in (None, device)))
    ordered = sorted(runs, key=lambda key: runs[key]['first'])
    groups = []
    # Group before applying filters: a different device/version must not become
    # an invisible bridge joining two unrelated tests of the requested device.
    for key in ordered:
        run = runs[key]
        if (not groups or groups[-1]['version'] != run['version'] or
                (groups[-1]['device_ref'] and run['device_ref'] and
                 groups[-1]['device_ref'] != run['device_ref']) or
                _gap(run['first'], groups[-1]['last']) > ROUND_GAP_SECONDS or
                any(row['version'] != run['version'] and
                    groups[-1]['last'] < row['time'] <= run['first'] for row in starts)):
            groups.append(dict(version=run['version'], device_ref=run['device_ref'],
                               first=run['first'], last=run['last'], ids=[]))
        group = groups[-1]
        group['ids'].append(key)
        group['last'] = max(group['last'], run['last'])
        group['device_ref'] = group['device_ref'] or run['device_ref']

    explicit_time = lower is not None or upper is not None
    matching_starts = [row for row in starts if not version or row['version'] == version]
    latest_start = matching_starts[-1] if matching_starts else None
    overlapping = []
    expanded = []
    if explicit_time:
        overlapping = [key for key in ordered if eligible(runs[key]) and
                    (not lower or runs[key]['last'] >= lower) and
                    (not upper or runs[key]['first'] <= upper)]
        touched = set(overlapping)
        selected = [key for group in groups if touched.intersection(group['ids'])
                    for key in group['ids'] if eligible(runs[key])]
        expanded = [key for key in selected if key not in touched]
        if expanded:
            warnings.append('specified_window_expanded_to_inferred_round_review_added_runs')
    else:
        # A group's known device must not hide its unbound runs. Their device
        # remains unknown even when a nearby run belongs to another device.
        candidates = [g for g in groups if any(eligible(runs[key]) for key in g['ids'])]
        group = candidates[-1] if candidates else None
        selected = [key for key in group['ids'] if eligible(runs[key])] if group else []
        # Opening a newer program without starting its receiver must not make
        # old failures look like failures in that new program.
        if latest_start and (not group or (latest_start['time'] > group['last'] and
                (latest_start['version'] != group['version'] or
                 _gap(latest_start['time'], group['last']) > ROUND_GAP_SECONDS))):
            selected = []
        warnings.append('latest_round_inferred_review_time_version_device')
        if selected and selected[-1] != ordered[-1]:
            warnings.append('matching_round_is_older_than_latest_receiver_run')

    selected_runs = {key: runs[key] for key in selected}
    all_times = [stamp for rows in line_index.values() for _, stamp, _ in rows if stamp]
    first = min([runs[key]['first'] for key in selected] + ([lower] if lower else []), default=None)
    last = max([runs[key]['last'] for key in selected] + ([upper] if upper else []), default=None)
    if first is None:
        first = latest_start['time'] if latest_start else min(all_times, default=None)
    if last is None:
        last = max(all_times, default=first)
    if not explicit_time and not selected:
        warnings.append('no_receiver_runs_in_latest_candidate_read_startup_context')
    elif (not explicit_time and not device and latest_start and selected and
          latest_start['time'] > runs[selected[-1]]['last']):
        # A nearby restart with no receiver logs is still part of the proposed
        # test round. Keep its startup/error context rather than hiding it.
        last = max(last, latest_start['time'])
        warnings.append('latest_startup_has_no_receiver_run')
    if version and not selected and not matching_starts:
        first = last = None
        warnings.append('requested_version_not_found')
    if device and not any(r['device_ref'] == device for r in selected_runs.values()):
        warnings.append('requested_device_not_confirmed_in_scope')
    if selected and any(r['device_ref'] is None for r in selected_runs.values()):
        warnings.append('some_runs_have_no_device_binding')
    if selected:
        warnings.append('version_and_device_association_requires_context_check')
    if not selected:
        warnings.append('no_matching_runs_does_not_mean_no_failure')
    if (first and last and (first > last or (explicit_time and not selected and
            not any(first <= stamp <= last for stamp in all_times)))):
        first = last = None
        warnings.append('requested_window_has_no_available_records')

    # Include the startup prelude and untimestamped exception continuations.
    # Keep the requested interval separately: expansion is visible, not silent.
    anchor = runs[selected[0]]['first'] if selected else first
    prelude = [row for row in starts if anchor and row['time'] <= anchor]
    startup = prelude[-1] if prelude else None
    expected_version = runs[selected[0]]['version'] if selected else version
    if startup and (not expected_version or startup['version'] == expected_version):
        between = [key for key in ordered if key not in selected and
                   startup['time'] <= runs[key]['first'] < anchor]
        if not between:
            first = min(first, startup['time'])
    else:
        warnings.append('startup_boundary_missing_or_rotated_out')
    # Capture cleanup/system queries after the final run up to the next
    # unrelated run/startup. Explicit windows also keep whole overlapping runs.
    boundaries = ([runs[key]['first'] for key in ordered if key not in selected and
                   last and runs[key]['first'] > last] +
                  [row['time'] for row in starts if last and row['time'] > last])
    stop_before = min(boundaries, default=None)
    tail = [stamp for stamp in all_times if last and stamp >= last and
            (stop_before is None or stamp < stop_before)]
    if tail:
        last = max(tail)
    inside = lambda stamp: bool(stamp and first and last and first <= stamp <= last)
    ranges, trace_events = [], {}
    for name, rows in line_index.items():
        included = [(number, stamp, event) for number, stamp, event in rows if inside(stamp)]
        if included:
            numbers = [number for number, _, _ in included]
            ranges.append(dict(name=name, first_line=min(numbers), last_line=max(numbers),
                               matching_lines=len(numbers)))
            if TRACE.fullmatch(name):
                trace_events[name] = dict(Counter(event for _, _, event in included))
        if any(stamp is None for _, stamp, _ in rows):
            warnings.append(f'unscoped_lines:{name}')
    if not ranges:
        warnings.append('scope_has_no_available_records')
    overlapping_other_runs = [key for key in ordered if key not in selected and first and last and
                              runs[key]['first'] <= last and runs[key]['last'] >= first]
    if overlapping_other_runs:
        warnings.append('context_contains_other_runs_do_not_attribute_all_to_selected_device')
    findings = [row for row in result['application_findings'] if inside(row['time'])]
    def epoch_stamp(value):
        if zone is None or not isinstance(value, (int, float)):
            return None
        try:
            return datetime.fromtimestamp(value, zone).strftime('%Y-%m-%d %H:%M:%S,%f')[:23]
        except (ValueError, OverflowError, OSError):
            return None
    incidents = []
    for row in result['report_incidents']:
        begin, end = epoch_stamp(row['started_at']), epoch_stamp(row['last_failure_at'])
        if begin is None or end is None:
            warnings.append('unscoped_report_incident')
        elif first and last and begin <= last and end >= first:
            incidents.append(row)
    focus_versions = sorted({r['version'] for r in selected_runs.values()})
    if (not explicit_time and result['export_version'] and focus_versions and
            result['export_version'] not in focus_versions):
        warnings.append('export_version_differs_from_selected_runs')
    return dict(basis='specified_window' if explicit_time else 'inferred_latest_round',
                requested=dict(since=lower, until=upper, version=version, device=device),
                inference_gap_seconds=ROUND_GAP_SECONDS,
                window_overlapping_runs=overlapping, round_expansion_runs=expanded,
                first=first, last=last, startup_context=startup,
                versions=focus_versions, run_count=len(selected), runs=selected_runs,
                other_runs_in_context=overlapping_other_runs, source_ranges=ranges,
                application_findings=findings, trace_events=trace_events, report_incidents=incidents,
                warnings=sorted(set(warnings)),
                interpretation='read_whole_selected_runs_and_context_not_only_failure_index')


def focused_output(result):
    """CLI defaults to the current scope; full metadata stays opt-in."""
    return dict(archive=result['archive'], focus=result['focus'],
                history_summary=dict(total_runs=len(result['runs']),
                    outside_focus_runs=len(result['runs'])-result['focus']['run_count'],
                    versions={key: len(rows) for key, rows in result['versions'].items()}),
                export_version=result['export_version'], exported_at=result['exported_at'],
                sha256=result['sha256'], inventory=result['inventory'],
                limitations=result['limitations'], all_members_accounted=result['all_members_accounted'])


def audit(path, *, since=None, until=None, version=None, device=None):
    result = dict(archive=Path(path).name, sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                  inventory=[], limitations=[], versions={}, runs={}, application_findings=[],
                  trace_events={}, trace_contexts=[], report_incidents=[])
    parsed, text_records, line_index, trace_index = {}, [], {}, {}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 128 or sum(i.file_size for i in infos) > 128*1024*1024:
            raise ValueError('archive_limit_exceeded')
        names = Counter(i.filename for i in infos)
        for name, count in names.items():
            if count > 1:
                result['limitations'].append(f'duplicate_member:{name}')
        for info in infos:
            row = dict(name=info.filename, bytes=info.file_size)
            result['inventory'].append(row)
            if info.file_size > 16*1024*1024:
                row['status'] = 'size_limit'
                result['limitations'].append(f'unread:{info.filename}')
                continue
            data = archive.read(info)  # CRC is checked by ZipFile, no extraction.
            row['sha256'] = hashlib.sha256(data).hexdigest()
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                row['status'] = 'encoding_error'
                result['limitations'].append(f'unread:{info.filename}')
                continue
            lines = text.splitlines()
            row.update(lines=len(lines), status='read', accounted_lines=0)
            if TEXT_LOG.fullmatch(info.filename):
                continuation, levels, times = 0, Counter(), []
                last_stamp, indexed = None, []
                for number, line in enumerate(lines, 1):
                    match = STAMP.match(line)
                    if match:
                        last_stamp = match[1]
                        times.append(match[1]); levels[match[2]] += 1
                        text_records.append((match[1], info.filename, number, line))
                    else:
                        continuation += 1
                    indexed.append((number, last_stamp, None))
                line_index[info.filename] = indexed
                row.update(accounted_lines=len(lines), timestamped_lines=len(times), continuation_lines=continuation,
                           levels=dict(levels), first=times[0] if times else None, last=times[-1] if times else None)
            elif TRACE.fullmatch(info.filename):
                events, invalid = Counter(), []
                indexed = []
                for number, line in enumerate(lines, 1):
                    try:
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            raise ValueError('not_object')
                        events[str(item.get('event', 'missing_event'))] += 1
                        indexed.append((number, item.get('wall_time'), str(item.get('event', 'missing_event'))))
                        if item.get('event') == 'device_context':
                            result['trace_contexts'].append(dict(source=f'{info.filename}:{number}',
                                wall_time=item.get('wall_time'), version=item.get('app_version'),
                                device_ref=item.get('selected_ref'), session_id=item.get('session_id')))
                    except ValueError:
                        invalid.append(number)
                        indexed.append((number, None, 'invalid_json'))
                trace_index[info.filename] = indexed
                row.update(accounted_lines=len(lines), events=dict(events), invalid_json_lines=invalid)
                result['trace_events'][info.filename] = dict(events)
                if invalid:
                    result['limitations'].append(f'invalid_trace:{info.filename}')
            elif info.filename in ('export-info.json', 'diagnostic-report.json'):
                try:
                    parsed[info.filename] = json.loads(text)
                    if not isinstance(parsed[info.filename], dict):
                        parsed.pop(info.filename)
                        raise ValueError('not_object')
                    row['accounted_lines'] = len(lines)
                except ValueError:
                    row['status'] = 'invalid_json'
                    result['limitations'].append(f'invalid_json:{info.filename}')
            else:
                row['status'] = 'unclassified_member'
                result['limitations'].append(f'unclassified:{info.filename}')

    manifest = parsed.get('export-info.json', {})
    result['export_version'] = manifest.get('app_version')
    result['exported_at'] = manifest.get('exported_at')
    try:
        zone = datetime.fromisoformat(result['exported_at']).tzinfo
    except (TypeError, ValueError):
        zone = None
    for name, rows in trace_index.items():
        indexed = []
        for number, wall_time, event in rows:
            stamp = None
            if zone is not None and isinstance(wall_time, (int, float)):
                try:
                    stamp = datetime.fromtimestamp(wall_time, zone).strftime('%Y-%m-%d %H:%M:%S,%f')[:23]
                except (ValueError, OverflowError, OSError):
                    pass
            indexed.append((number, stamp, event))
        line_index[name] = indexed
    if not manifest:
        result['limitations'].append('missing_manifest')
    for key in ('application_log_flushed', 'diagnostic_trace_flushed', 'fault_report_flushed'):
        if manifest.get(key) is not True:
            result['limitations'].append(f'flush_unconfirmed:{key}')
    if manifest.get('snapshot_stable') is False:
        result['limitations'].append('export_changed_during_collection')
    if manifest.get('incomplete') is True:
        result['limitations'].append('export_marked_incomplete')
    inventory = {r['name']: r for r in result['inventory']}
    for item in manifest.get('files', []):
        name = item.get('name')
        if item.get('status') == 'unreadable' or item.get('truncated') or item.get('changed_during_read'):
            result['limitations'].append(f'export_incomplete:{name}')
        if item.get('status') == 'included' and (name not in inventory or
                inventory[name]['bytes'] != item.get('exported_bytes')):
            result['limitations'].append(f'manifest_mismatch:{name}')

    current_version, current_start = 'unidentified', None
    for timestamp, name, number, line in sorted(text_records):
        location = f'{name}:{number}'
        match = VERSION.search(line)
        if match:
            current_version = match[1]
            current_start = timestamp
            result['versions'].setdefault(current_version, []).append(dict(time=timestamp, source=location))
        # Old versions log some worker failures without a run ID. Index them
        # explicitly rather than silently treating an empty run failure list as
        # success. The original full line remains at the reported source.
        if (re.search(r'\b(?:WARNING|ERROR|CRITICAL)\b', line) or
                re.search(r'phase=failed|query_timeout|query_failed|AccessDenied|exit_hex=', line)):
            result['application_findings'].append(dict(source=location, time=timestamp, version=current_version,
                metadata=dict(re.findall(r'\b(stage|phase|reason|exit_hex|code)=([A-Za-z0-9_.-]+)',line))))
        match = RUN.search(line)
        if not match:
            continue
        run = result['runs'].setdefault(match[1], dict(version=current_version,
            version_context_time=current_start,
            version_relation='preceding_app_identity_check_for_overlapping_processes', mode='unidentified', first=timestamp, last=timestamp,
            lines=0, kinds={}, stages={}, failures=[], diagnostics=[], exits=[], delivery=[],
            evidence_sequences=[], final_summary=False, counters={}))
        run['last'] = timestamp
        run['lines'] += 1
        mode = re.search(r'\bmode=(run|detect)\b', line)
        if mode:
            run['mode'] = mode[1]
        if ' record=' not in line:
            # Index every worker diagnostic/exit, including versions predating
            # structured startup evidence. Keep fixed key/value metadata only.
            fields = dict(re.findall(r'\b(stage|phase|reason|exit_hex|last_worker_stage|last_worker_state|code)=([A-Za-z0-9_.-]+)', line))
            if fields:
                bucket = 'exits' if 'exit_hex' in fields else 'diagnostics'
                run[bucket].append(dict(source=location, time=timestamp, **fields))
            if ' counts=' in line and 'input delivery' in line:
                try:
                    run['delivery'].append(dict(source=location, counts=json.loads(line.split(' counts=',1)[1])))
                except ValueError:
                    result['limitations'].append(f'invalid_delivery:{location}')
        if ' record=' in line:
            try:
                record = json.loads(line.split(' record=', 1)[1])
                kind = record.get('kind', 'unknown')
                run['kinds'][kind] = run['kinds'].get(kind, 0) + 1
                seq = re.search(r'\bseq=(\d+)\b', line)
                if seq:
                    run['evidence_sequences'].append(int(seq[1]))
                if kind == 'stage':
                    stage = record.get('stage', 'unknown')
                    if stage in ('run', 'detect'):
                        run['mode'] = stage
                    run['stages'][stage] = run['stages'].get(stage, 0) + 1
                if record.get('state') == 'failed' or record.get('outcome') in ('status_failed', 'timeout', 'os_error', 'exception'):
                    # Metadata records only; raw application messages stay in source.
                    run['failures'].append(dict(source=location, time=timestamp, record=record))
                if kind == 'summary':
                    run['final_summary'] |= bool(record.get('final'))
                    run['counters'] = record.get('counts', {})
            except (ValueError, AttributeError):
                result['limitations'].append(f'invalid_evidence:{location}')
    for run in result['runs'].values():
        sequences = run.pop('evidence_sequences')
        # IPC seq includes other message types; gaps alone are not evidence loss.
        run['evidence_seq_first'] = min(sequences) if sequences else None
        run['evidence_seq_last'] = max(sequences) if sequences else None
        run['evidence_seq_duplicates'] = len(sequences)-len(set(sequences))
        run['device_ref'] = None
        try:
            if zone is None:
                continue  # No local-PC timezone guesses for another user's logs.
            stamp = datetime.strptime(run['first'], '%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=zone).timestamp()
            startup_stamp = (datetime.strptime(run['version_context_time'], '%Y-%m-%d %H:%M:%S,%f')
                .replace(tzinfo=zone).timestamp() if run['version_context_time'] else None)
            # Trace and text startup records can round the same millisecond
            # differently. Tolerate that only at the startup boundary; never
            # associate a context recorded after the receiver run began.
            contexts = [c for c in result['trace_contexts'] if isinstance(c['wall_time'], (int,float))
                        and c['wall_time'] <= stamp and c['version'] == run['version']
                        and (startup_stamp is None or c['wall_time'] >= startup_stamp - .001)]
            if contexts:
                context = max(contexts, key=lambda c:c['wall_time'])
                run['device_ref'] = context['device_ref']
                run['device_context_source'] = context['source']
                run['context_relation'] = 'preceding_trace_same_version_not_authenticated_run_binding'
        except (TypeError, ValueError):
            pass
    report = parsed.get('diagnostic-report.json', {})
    for incident in report.get('incidents', []):
        row = {key: incident.get(key) for key in ('session_id', 'started_at', 'last_failure_at',
               'failure_count', 'failure_key', 'dropped_after', 'before_limited')}
        result['report_incidents'].append(row)
        if row.get('dropped_after') or row.get('before_limited'):
            result['limitations'].append('incident_window_limited_read_application_log')
    result['limitations'] = sorted(set(result['limitations']))
    result['all_members_accounted'] = all(r['status'] == 'read' and r.get('lines') == r.get('accounted_lines')
                                         for r in result['inventory'])
    result['focus'] = _focus(result, line_index, since=since, until=until,
                             version=version, device=device, zone=zone)
    result['interpretation'] = 'inventory_all_members_read_focus_first_history_only_when_relevant'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='+', type=Path)
    parser.add_argument('--since', help='反馈起始时间，ISO 日期时间；无时区时按日志当地时间')
    parser.add_argument('--until', help='反馈结束时间；重叠运行自动保留完整起止过程')
    parser.add_argument('--version', help='实际程序完整版本号，不以导出版本替代')
    parser.add_argument('--device', help='设备短编号；无法绑定的运行仍保留并提示')
    parser.add_argument('--all-history', action='store_true', help='同时输出完整历史索引；默认仅输出本次范围和历史摘要')
    args = parser.parse_args()
    for path in args.archives:
        try:
            result = audit(path, since=args.since, until=args.until, version=args.version, device=args.device)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result if args.all_history else focused_output(result), ensure_ascii=False))
