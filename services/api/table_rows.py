"""Bounded-memory raw-table pagination for the run and analysis data browser.

CSV type choices are derived across all chunks before page selection, so a late
zero-padded identifier or nonnumeric value cannot change an earlier page's type.
Ordinary sorted pages retain only their top candidates; deep pages use
a disposable disk-backed SQLite table, never a whole-file DataFrame.
"""
from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .common import INTEGER_LITERAL, infer_csv_column_types


_BATCH = 2048


def _raw_batches(path: Path):
    import pandas as pd
    if path.suffix.lower() == '.parquet':
        import pyarrow.parquet as pq
        with pq.ParquetFile(path) as table:
            for batch in table.iter_batches(batch_size=_BATCH):
                yield batch.to_pandas()
    else:
        with pd.read_csv(path, dtype=str, keep_default_na=False,
                         na_values=[''], chunksize=_BATCH) as chunks:
            yield from chunks


def _csv_types(path: Path):
    """Aggregate chunk-level inference, including global precision hazards."""
    import pandas as pd
    columns = list(pd.read_csv(path, nrows=0).columns)
    kinds = {name: set() for name in columns}
    has_big_int = {name: False for name in columns}
    has_null = {name: False for name in columns}
    for raw in _raw_batches(path):
        for name in columns:
            series = raw[name]
            has_null[name] |= bool(series.isna().any())
            values = series.dropna()
            if values.empty:
                continue
            inferred = infer_csv_column_types(raw[[name]].copy())[name]
            if values.isin(('True', 'False')).all():
                kinds[name].add('bool')
            elif pd.api.types.is_numeric_dtype(inferred):
                kinds[name].add('float' if pd.api.types.is_float_dtype(inferred) else 'int')
                long_literals = values[values.str.len().gt(15) & values.str.fullmatch(INTEGER_LITERAL)]
                if not long_literals.empty and any(abs(int(value)) > 2**53 for value in long_literals):
                    has_big_int[name] = True
            else:
                kinds[name].add('text')
    result = {}
    for name in columns:
        options = kinds[name]
        if not options or options == {'text'} or 'text' in options:
            result[name] = 'text'
        elif options == {'bool'}:
            result[name] = 'bool'
        elif options <= {'int', 'float'}:
            result[name] = ('text' if has_big_int[name] and ('float' in options or has_null[name])
                            else 'float' if 'float' in options or has_null[name] else 'int')
        else:
            result[name] = 'text'
    return columns, result


def _typed_batches(path: Path, kinds):
    import pandas as pd
    for raw in _raw_batches(path):
        if kinds is not None:
            for name, kind in kinds.items():
                if kind == 'bool':
                    raw[name] = raw[name].map({'True': True, 'False': False})
                elif kind in ('int', 'float'):
                    values = pd.to_numeric(raw[name].replace({
                        'nan': float('nan'), 'NaN': float('nan'),
                        '-nan': float('nan'), '-NaN': float('nan'),
                    }))
                    raw[name] = values.astype(float) if kind == 'float' else values
        yield raw


def _json_rows(frame):
    """Match the existing API's dates, nulls and exact finite-float response."""
    rows = json.loads(frame.to_json(orient='records'))
    for row, values in zip(rows, frame.to_dict(orient='records')):
        for column, value in values.items():
            if isinstance(value, float) and math.isfinite(value):
                row[column] = value
    return rows


def _order_value(value):
    import pandas as pd
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return (1, None, None)
    if isinstance(value, (int, float, bool)):
        return (0, value, None)
    if hasattr(value, 'isoformat'):
        return (0, None, value.isoformat())
    return (0, None, str(value))


def table_rows(path: Path, offset: int, limit: int, filter_text: str, sort: str, descending: bool):
    import pandas as pd
    if path.suffix.lower() == '.parquet':
        import pyarrow.parquet as pq
        import pyarrow as pa
        with pq.ParquetFile(path) as table:
            schema = table.schema_arrow
            columns = list(schema.names)
        kinds = None
    else:
        columns, kinds = _csv_types(path)
    start = max(0, offset)
    count = max(0, min(limit, 1000))
    sorting = sort in columns
    total = 0
    selected = []
    if sorting and start + count <= 8192 and (start + count) * len(columns) <= 100_000:
        # Ordinary UI pages need only the best (offset + limit) records. Keep
        # those rows, not an index or materialized copy of the entire table.
        leaders = None
        for frame in _typed_batches(path, kinds):
            if filter_text:
                mask = frame.astype(str).apply(
                    lambda col: col.str.contains(filter_text, case=False, regex=False)).any(axis=1)
                frame = frame[mask]
            total += len(frame)
            if frame.empty or not count:
                continue
            local = frame.sort_values(sort, ascending=not descending,
                                      kind='stable', na_position='last').head(start + count)
            leaders = (local if leaders is None else
                       pd.concat([leaders, local], ignore_index=True)
                       .sort_values(sort, ascending=not descending, kind='stable',
                                    na_position='last').head(start + count))
        if leaders is None:
            return columns, [], total
        return columns, _json_rows(leaders.iloc[start:start + count]), total
    if sorting:
        # Keep large-offset sort scratch on the same disk as the workspace,
        # not in a potentially memory-backed system /tmp filesystem.
        from .config import settings
        scratch = settings.data_dir / '.scratch'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='table-rows-', dir=scratch) as directory:
            with closing(sqlite3.connect(str(Path(directory) / 'pages.db'))) as db:
                db.execute('PRAGMA temp_store=FILE')
                db.execute('PRAGMA cache_size=-4096')
                db.execute('PRAGMA journal_mode=OFF')
                db.execute('PRAGMA synchronous=OFF')
                db.execute('CREATE TABLE records (ordinal INTEGER PRIMARY KEY, missing INTEGER, number NUMERIC, text TEXT, payload TEXT)')
                for frame in _typed_batches(path, kinds):
                    if filter_text:
                        matches = frame.astype(str).apply(lambda c: c.str.contains(filter_text, case=False, regex=False)).any(axis=1)
                        frame = frame[matches]
                    if frame.empty:
                        continue
                    values = frame[sort].tolist()
                    encoded = _json_rows(frame)
                    records = []
                    for value, row in zip(values, encoded):
                        missing, number, text = _order_value(value)
                        records.append((total + len(records), missing, number, text, json.dumps(row, ensure_ascii=False)))
                    db.executemany('INSERT INTO records VALUES (?, ?, ?, ?, ?)', records)
                    total += len(records)
                order = 'DESC' if descending else 'ASC'
                field = 'text' if kinds is not None and kinds[sort] == 'text' else 'number'
                # Parquet and CSV string columns sort lexicographically, numeric columns numerically.
                if kinds is None:
                    arrow_type = schema.field(sort).type
                    numeric = (pa.types.is_integer(arrow_type) or pa.types.is_floating(arrow_type)
                               or pa.types.is_boolean(arrow_type))
                    field = 'number' if numeric else 'text'
                rows = [json.loads(item[0]) for item in db.execute(
                    f'SELECT payload FROM records ORDER BY missing ASC, {field} {order}, ordinal ASC LIMIT ? OFFSET ?',
                    (count, start))]
                return columns, rows, total
    for frame in _typed_batches(path, kinds):
        if filter_text:
            matches = frame.astype(str).apply(lambda c: c.str.contains(filter_text, case=False, regex=False)).any(axis=1)
            frame = frame[matches]
        chunk_size = len(frame)
        if chunk_size and total + chunk_size > start and len(selected) < count:
            begin = max(0, start - total)
            selected.extend(_json_rows(frame.iloc[begin:begin + count - len(selected)]))
        total += chunk_size
    return columns, selected, total
