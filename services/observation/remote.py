"""Saved remote observations from the owning execution adapter; no SSH on reads."""
from sqlalchemy import select,func
from services.api.db import Session, TaskRun, now
from .models import ObservationScope, ObservedFile
from .files import allowed, sync_scopes, PROJECT_CONTENT_LIMIT, PROJECT_FILE_LIMIT
from .segments import index
from .reporting import write_lock, ensure_state
from services.api.common import get
from services.api.db import Project

def capture(run_id, observation, attempt_id=None):
    if not run_id: return
    with Session.begin() as s:
        write_lock(s); run=s.get(TaskRun,run_id)
        if not run: return
        get(s,Project,run.project_id,for_update=True)
        s.refresh(run)
        if (run.config.get('execution_attempt') or {}).get('id')!=attempt_id: return
        ensure_state(s,run.project_id); sync_scopes(s,run.project_id,recent_only=True)
        scope=s.scalar(select(ObservationScope).where(ObservationScope.project_id==run.project_id,ObservationScope.kind=='remote_workspace',ObservationScope.object_id==run_id))
        if not scope: return
        if not observation:
            scope.coverage='unavailable'; scope.error='Remote host disconnected; last observations retained'; return
        # Deletion is destructive. Only an explicit successful traversal of the
        # remote root and every visited subtree can certify absence. Older
        # adapters without scan_ok fail closed rather than deleting last-known
        # source bytes on an os.walk error or a missing workspace mount.
        if not isinstance(observation, dict) or observation.get('scan_ok') is not True or observation.get('error'):
            scope.coverage='unavailable'; scope.error='Remote inventory incomplete; restore workspace access and retry'; return
        if observation.get('cursor_found') is not True:
            scope.coverage='partial'; scope.error='Remote inventory cursor disappeared; next pass restarts'; return
        files = observation.get('files')
        more = observation.get('has_more')
        if (not isinstance(files, list) or len(files)>200 or type(more) is not bool
                or not isinstance(observation.get('cursor'), str)
                or any(not isinstance(item, dict) or not isinstance(item.get('path'), str)
                       or type(item.get('size')) is not int
                       or type(item.get('mtime_ns')) is not int for item in files)
                or (more and (not files or observation.get('next_cursor') != files[-1].get('path')))):
            scope.coverage='partial'; scope.error='Remote inventory page invalid; last observations retained'; return
        scope.coverage='partial' if more else 'complete'; scope.error=None
        scope.observed_at=now()
        if not observation.get('cursor'): scope.scan_generation+=1
        total=s.scalar(select(func.coalesce(func.sum(func.length(ObservedFile.content)),0)).where(ObservedFile.project_id==run.project_id)) or 0
        count=s.scalar(select(func.count()).select_from(ObservedFile).where(ObservedFile.project_id==run.project_id)) or 0
        for item in files:
            relative=item.get('path','')
            if not allowed(relative): continue
            row=s.scalar(select(ObservedFile).where(ObservedFile.scope_id==scope.id,ObservedFile.path==relative))
            if row is None:
                if count>=PROJECT_FILE_LIMIT:
                    scope.coverage='partial'; scope.error='Project inventory capacity reached'; continue
                count+=1
                row=ObservedFile(project_id=run.project_id,scope_id=scope.id,path=relative,generation=0); s.add(row)
            content=item.get('content'); content=content.encode('utf-8') if isinstance(content,str) else None
            if content is not None and total-len(row.content or b'')+len(content)>PROJECT_CONTENT_LIMIT: content=None
            total=total-len(row.content or b'')+len(content or b'')
            metadata={'size':int(item.get('size',0)),'mtime_ns':int(item.get('mtime_ns',0))}
            if row.content!=content or row.stat!=metadata or row.state=='deleted':
                row.generation=(row.generation or 0)+1; row.content=content
                row.segments,row.parse_state=index(content,relative) if content is not None else ([], 'metadata_only')
            row.stat=metadata; row.size=metadata['size']; row.state='available' if content is not None else 'metadata_only'
            row.scan_generation=scope.scan_generation; row.attempt_id=scope.attempt_id
            row.attribution='run_process_scope'; row.observed_at=scope.observed_at
        if not more and not scope.error:
            for row in s.scalars(select(ObservedFile).where(ObservedFile.scope_id==scope.id,ObservedFile.scan_generation!=scope.scan_generation)):
                if row.state!='deleted': row.generation+=1
                row.state='deleted'; row.content=None; row.segments=[]; row.parse_state='unavailable'
