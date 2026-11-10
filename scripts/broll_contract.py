"""Frozen Shortform production requests and validated, revision-safe B-roll imports."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import subprocess

import sf_project
import sf_audio
from cutter_provider import atomic_json, file_lock, read_json

DEFAULT_PROFILE = Path.home() / 'Documents/Projects/broll-set-template/profiles/skaile-shortform/manifest.json'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def number(value, label):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(label + ' must be finite')
    return value


def _profile(path):
    path = Path(path).expanduser().resolve()
    doc = json.loads(path.read_text())
    if not doc.get('version') or doc.get('profile_id') != 'skaile-shortform':
        raise ValueError('Versioned skaile-shortform profile required')
    approved_path = Path(doc['approved_sfx_file'])
    if not approved_path.is_absolute():
        approved_path = path.parent / approved_path
    approved = json.loads(approved_path.read_text())
    assets = approved['assets']
    if not assets or len({a['id'] for a in assets}) != len(assets):
        raise ValueError('Approved SFX registry missing or ambiguous')
    return {'path': str(path), 'sha256': sha256(path), 'manifest': doc,
            'approved_sfx_path': str(approved_path), 'approved_sfx': approved}


def _source_words(workdir):
    """Prefer explicitly bound final ASR; never infer an axis from similar duration."""
    root = Path(workdir)
    alignment = read_json(root / 'cut_words_alignment.json') or {}
    name = alignment.get('words_file')
    if name in ('cut_words.json', 'cc_words.json'):
        from clip_register import words_digest
        words = read_json(root / name)
        if words and words_digest(words) == alignment.get('words_sha256'):
            result = []
            for w in words:
                mid = (float(w['start']) + float(w['end'])) / 2
                for segment in alignment.get('segments', []):
                    if segment['cut_start'] <= mid < segment['cut_end']:
                        shift = segment['in'] - segment['cut_start']
                        result.append(dict(w, start=float(w['start'])+shift, end=float(w['end'])+shift))
                        break
            return result, name + ':source-aligned'
    words = read_json(root / 'words_aai.json')
    if not isinstance(words, list) or not words:
        raise ValueError('Source-aligned word timestamps required before B-roll production')
    return words, 'words_aai.json'


def prepare_request(workdir, source_video, selected_range=None, profile_path=DEFAULT_PROFILE,
                    expected_revision=None, creative_request=""):
    root = Path(workdir).resolve()
    settings = sf_project.load_settings(root)
    if expected_revision and expected_revision != settings['revision']:
        raise sf_project.RevisionConflict('Schnitt wurde geändert. Erst speichern und neu starten.')
    source = sf_audio.source_identity(source_video)
    clips = settings['overrides'].get('timeline_clips') or []
    if not clips or len({c['id'] for c in clips}) != len(clips):
        raise ValueError('Unique confirmed timeline clips required')
    from rerender import load_pause_map
    pauses = load_pause_map(str(root))
    pause_identity = sf_audio.source_identity(root/'pause_map.json') if (root/'pause_map.json').exists() else None
    plan = sf_audio.timing_plan(clips, 60, pauses)
    words, word_source = _source_words(root)
    profile = _profile(profile_path)
    duration = max(p['cut_end'] for p in plan)
    if selected_range is not None:
        a, b = (number(x, 'selected_range') for x in selected_range)
        if a < 0 or b <= a or b > duration + 1/60:
            raise ValueError('Selected range outside current cut')
        selected_range = [a, min(b, duration)]
    out = []
    for i, p in enumerate(plan):
        clip = clips[i]
        role = 'hook' if i == 0 else ('cta' if i == len(clips)-1 else 'body')
        ws = []
        audio_in = p.get('audio_source_in', p['source_in'])
        audio_start = p['start_sample'] / sf_audio.SAMPLE_RATE
        for w in words:
            lo, hi = float(w['start']), float(w['end'])
            if float(clip['in']) <= (lo+hi)/2 < float(clip['out']):
                ws.append(dict(w, source_start=lo, source_end=hi,
                               cut_start=audio_start+lo-audio_in, cut_end=audio_start+hi-audio_in))
        if not ws:
            raise ValueError('No aligned speech for clip ' + str(clip['id']))
        selected = selected_range is None or (p['cut_end'] > selected_range[0] and p['cut_start'] < selected_range[1])
        out.append({'clip_id': clip['id'], 'clip_idx': i, 'role': role,
                    'src_in': p['source_in'], 'src_out': p['source_out'],
                    'audio_source_in': audio_in, 'audio_cut_start': audio_start,
                    'cut_start': p['cut_start'], 'cut_end': p['cut_end'], 'duration': p['duration'],
                    'words': ws, 'text': ' '.join(w['text'] for w in ws), 'selected': selected,
                    'allowed_layouts': ['overhead'] if role != 'body' else ['overhead', 'fullscreen'],
                    'overrides': deepcopy(clip)})
    # Detect changes during word/profile reads before the job can be launched.
    if sf_project.load_settings(root)['revision'] != settings['revision']:
        raise sf_project.RevisionConflict('Schnitt wurde während der Auftragserstellung geändert.')
    if sf_audio.source_identity(source_video) != source:
        raise ValueError('Source changed during snapshot')
    audio = settings['layout'].get('audio', {})
    microphone = sf_audio.voice_source_identity(root, source_video, audio) if audio.get('source_file') else None
    doc = {'schema_version': 2, 'workdir': str(root), 'profile': profile, 'source': source,
           'microphone': microphone, 'settings': settings, 'settings_revision': settings['revision'],
           'fps': 60, 'duration': duration, 'clips': out, 'selected_range': selected_range,
           'word_source': word_source, 'pauses': pauses, 'pause_identity': pause_identity, 'creative_request': str(creative_request or '')}
    assert_no_existing_media_overlap(settings, selected_range or [0,duration], plan)
    doc['snapshot_id'] = sf_project.digest(doc)
    doc['auftraege'] = [dict(nr=i+1, clip_idx=c['clip_idx'], clip_id=c['clip_id'], role=c['role'],
                            src_in=c['src_in'], src_out=c['src_out'], clip_dur=c['duration'],
                            cut_start=c['cut_start'], cut_end=c['cut_end'], speed_fit=1,
                            text=c['text'], words=c['words'], out='broll/'+doc['snapshot_id'][:12]+'-%02d.mp4'%i)
                        for i,c in enumerate(out) if c['selected']]
    doc.update(clip_count=len(out), auftrag_count=len(doc['auftraege']), uebersprungen=[])
    return doc


def assert_no_existing_media_overlap(settings, interval, timing=None):
    """A new production cannot invisibly cover or double an existing manual layer."""
    a,b=interval
    clips=settings['overrides'].get('timeline_clips',[])
    plan={p['clip'].get('id'):p for p in (timing if timing is not None else sf_audio.timing_plan(clips,60))}
    for slot in settings['overrides'].get('broll',[]) or []:
        if not slot.get('file'):continue
        p=plan.get(slot.get('clip_id'))
        lo=slot.get('start',p['cut_start'] if p else None)
        hi=slot.get('end',p['cut_end'] if p else None)
        if lo is None or hi is None or (float(hi)>a and float(lo)<b):
            raise ValueError('Im gewählten Bereich liegt bereits B-Roll. Bitte einen freien Bereich wählen oder bestehende Ebenen zuerst selbst entfernen.')
    hook_end=next(iter(plan.values()))['cut_end'] if plan else 0
    if a<hook_end and b>0 and any(isinstance(v,dict) and v.get('file') for v in settings['layout'].get('hook',{}).values()):
        raise ValueError('Im Hook liegt bereits ein manuelles Bild. Vor neuer Hook-Produktion bitte selbst entfernen.')
    audio=settings['layout'].get('audio',{})
    if audio.get('sfx_file'):
        raise ValueError('Bestehende globale SFX-Spur zuerst prüfen; neuer Auftrag könnte Ton doppeln.')
    for event in audio.get('sfx_events',[]) or []:
        p=plan.get(event.get('clip_id'))
        if p:
            lo=p['cut_start']+float(event.get('offset',0));hi=lo+float(event.get('duration',p['duration']))
            if hi>a and lo<b:
                raise ValueError('Im gewählten Bereich liegen bereits SFX. Bestehende Effekte zuerst prüfen.')


def verify_request(request):
    excluded={'snapshot_id','auftraege','clip_count','auftrag_count','uebersprungen','job_id','result_path'}
    core={key:value for key,value in request.items() if key not in excluded}
    if request.get('schema_version')!=2 or sf_project.digest(core)!=request.get('snapshot_id'):
        raise ValueError('Frozen request integrity check failed')


def assert_snapshot_current(workdir, request):
    verify_request(request)
    if sf_project.load_settings(workdir)['revision'] != request['settings_revision']:
        raise sf_project.RevisionConflict('Projekt wurde seit dem B-Roll-Auftrag geändert. Ergebnis bleibt gesichert.')
    if sf_audio.source_identity(request['source']['path']) != request['source']:
        raise sf_project.RevisionConflict('Quellvideo wurde seit dem B-Roll-Auftrag geändert.')
    if request.get('pause_identity') and sf_audio.source_identity(request['pause_identity']['path']) != request['pause_identity']:
        raise sf_project.RevisionConflict('Pausenkarte wurde seit dem B-Roll-Auftrag geändert.')
    if request.get('microphone') and sf_audio.source_identity(request['microphone']['path']) != request['microphone']:
        raise sf_project.RevisionConflict('Mikrofonquelle wurde seit dem B-Roll-Auftrag geändert.')


def _media(file, checksum, root, probe):
    path = Path(file).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if '.part' in path.name or not path.is_file() or path.stat().st_size < 100:
        raise ValueError('Missing or incomplete media: ' + str(path))
    identity=sf_audio.source_identity(path)
    if not checksum or sha256(path) != checksum:
        raise ValueError('Media checksum mismatch: ' + str(path))
    metadata=probe(path)
    if sf_audio.source_identity(path)!=identity:
        raise ValueError('Media changed during validation: '+str(path))
    return str(path), metadata


def probe_media(path):
    p = subprocess.run(['ffprobe','-v','error','-count_frames','-show_streams','-show_format','-of','json',str(path)],
                       capture_output=True, text=True, check=True, timeout=30)
    return json.loads(p.stdout)


def _validate_result(request, result, probe=probe_media):
    verify_request(request)
    if result.get('schema_version') != 2 or result.get('snapshot_id') != request['snapshot_id']:
        raise ValueError('Result does not match frozen snapshot')
    if result.get('audio_owner') != 'separate_sfx':
        raise ValueError('One separate SFX owner required; video must be silent')
    root = Path(request['workdir'])
    if not result.get('sfx_events') and not str(result.get('silence_reason','')).strip():
        raise ValueError('SFX events or explicit artistic silence_reason required')
    scenes = result.get('scenes')
    if not isinstance(scenes,list) or not scenes:
        raise ValueError('Result scenes missing')
    clips = {c['clip_id']:c for c in request['clips']}
    ids, intervals, normalized = set(), [], []
    for scene in scenes:
        if not scene.get('id') or scene['id'] in ids:
            raise ValueError('Unique scene IDs required')
        ids.add(scene['id'])
        c = clips.get(scene['clip_id'])
        if not c or not c['selected']:
            raise ValueError('Scene outside selected clips')
        a,b = number(scene['start'],'scene.start'),number(scene['end'],'scene.end')
        if b<=a or a<c['cut_start']-1e-6 or b>c['cut_end']+1e-6:
            raise ValueError('Scene interval outside clip')
        selection=request.get('selected_range')
        if selection and (a<selection[0]-1e-6 or b>selection[1]+1e-6):
            raise ValueError('Scene interval outside requested range')
        layout=scene['layout']
        if layout not in c['allowed_layouts']:
            raise ValueError('Hook and CTA must stay overhead')
        file, media = _media(scene['file'],scene.get('sha256'),root,probe)
        video=[s for s in media['streams'] if s['codec_type']=='video']
        if len(video)!=1 or any(s['codec_type']=='audio' for s in media['streams']):
            raise ValueError('Exactly one silent video stream required')
        v=video[0]
        if (v['width'],v['height']) != (1080,768 if layout=='overhead' else 1920):
            raise ValueError('Scene dimensions do not match layout')
        duration=number(v.get('duration',media.get('format',{}).get('duration')),'media.duration')
        if abs(duration-(b-a))>2/30:
            raise ValueError('Scene duration must match final timing without speed-fit')
        if v.get('nb_read_frames') is not None:
            num,den=(number(n,'frame rate') for n in v.get('avg_frame_rate','0/1').split('/'))
            fps=num/den if den else 0
            if fps<29.9 or abs(number(v['nb_read_frames'],'frames')/fps-duration)>2/fps:
                raise ValueError('Incomplete or low-frame-rate video')
        intervals.append((a,b))
        normalized.append(dict(scene,file=file,start=a,end=b))
    intervals.sort()
    if any(a < prev[1]-1e-6 for prev,(a,b) in zip(intervals,intervals[1:])):
        raise ValueError('Result scenes overlap')
    # The request includes every selected clip, so a missing hook/CTA cannot pass silently.
    for c in clips.values():
        if c['selected'] and not any(s['clip_id']==c['clip_id'] for s in normalized):
            raise ValueError('Missing selected clip: '+str(c['clip_id']))
    approved={a['id']:a for a in request['profile']['approved_sfx']['assets'] if a.get('approved') is True}
    events=[]; ids=set(); audio_instances=set()
    for event in result.get('sfx_events',[]):
        if not event.get('id') or event['id'] in ids: raise ValueError('Unique SFX IDs required')
        ids.add(event['id'])
        c=clips.get(event['clip_id'])
        if not c or not c['selected']: raise ValueError('SFX outside selected clip')
        offset=number(event.get('offset',0),'SFX offset'); duration=number(event['duration'],'SFX duration')
        gain=number(event.get('gain_db',0),'SFX gain')
        if offset<0 or duration<=0 or offset+duration>c['duration']+1/60 or gain>0:
            raise ValueError('SFX interval or gain invalid')
        selection=request.get('selected_range')
        if selection and (c['cut_start']+offset<selection[0]-1e-6 or c['cut_start']+offset+duration>selection[1]+1e-6):
            raise ValueError('SFX outside requested range')
        if not event.get('cues'): raise ValueError('Approved SFX cue provenance required')
        for cue in event['cues']:
            asset=approved.get(cue['asset_id'])
            if not asset or cue.get('source_sha256')!=asset['sha256']:
                raise ValueError('Unapproved SFX source')
            if not cue.get('action'): raise ValueError('Visual action required for SFX cue')
            lo=number(cue['offset'],'cue.offset');dur=number(cue['duration'],'cue.duration')
            if lo<0 or dur<=0 or lo+dur>duration+1/60: raise ValueError('Cue outside SFX file')
        file,media=_media(event['file'],event.get('sha256'),root,probe)
        audios=[s for s in media['streams'] if s['codec_type']=='audio']
        if len(audios)!=1 or any(s['codec_type']=='video' for s in media['streams']):
            raise ValueError('SFX must contain one audio stream only')
        actual=number(audios[0].get('duration',media.get('format',{}).get('duration')),'audio.duration')
        if abs(actual-duration)>1/30: raise ValueError('SFX duration mismatch')
        instance=(c['clip_id'],round(offset*48000),event['sha256'])
        if instance in audio_instances: raise ValueError('Duplicate SFX audio instance')
        audio_instances.add(instance)
        events.append(dict(event,file=file,offset=offset,duration=duration,gain_db=gain,
                           source_anchor=c['src_in']))
    yoff=number(result.get('facecam_yoff',448),'facecam_yoff')
    if not 0<=yoff<=768: raise ValueError('Invalid facecam offset')
    return dict(result,scenes=normalized,sfx_events=events,facecam_yoff=yoff)


def validate_result(request, result, probe=probe_media):
    try:
        return _validate_result(request,result,probe)
    except (KeyError,TypeError,IndexError) as error:
        raise ValueError('Malformed B-roll result: '+str(error)) from error


def _job_directory(workdir,job_id):
    if not job_id or Path(str(job_id)).name != str(job_id) or str(job_id) in ('.','..'):
        raise ValueError('Invalid job ID')
    return Path(workdir).resolve()/'broll_jobs'/str(job_id)


def import_result(workdir, job_id, expected_revision=None):
    directory=_job_directory(workdir,job_id)
    with file_lock(directory/'.import.lock'):
        request=read_json(directory/'auftrag.json')
        if request.get('schema_version') != 2: raise ValueError('Legacy job uses existing import route')
        job=read_json(directory/'job.json')
        if job.get('status')!='done':
            raise ValueError('B-roll job has not completed validation')
        sf_project.recover_settings(workdir)
        current=sf_project.load_settings(workdir)
        receipt=read_json(directory/'imported.json')
        if receipt:
            return dict(current,imported=True,duplicate=True,job_id=job_id)
        stored_result=read_json(directory/'result.json')
        expected_slots={'broll-job-'+job_id+'-'+s['id'] for s in stored_result.get('scenes',[])}
        owned={s.get('slot_id') for s in current['overrides'].get('broll',[]) if s.get('broll_job_id')==job_id}
        if expected_slots and owned==expected_slots:
            atomic_json(directory/'imported.json',{'job_id':job_id,'snapshot_id':request['snapshot_id'],
                                                  'revision':current['revision'],'recovered':True})
            return dict(current,imported=True,duplicate=True,job_id=job_id)
        if expected_revision and expected_revision != current['revision']:
            raise sf_project.RevisionConflict('Cockpit ist veraltet. Bitte neu laden.')
        assert_snapshot_current(workdir,request)
        result=validate_result(request,read_json(directory/'result.json'))
        layout=deepcopy(current['layout']);ovr=deepcopy(current['overrides'])
        # No replacing arbitrary existing layers, even if they share a clip.
        slots=list(ovr.get('broll') or [])
        by_id={c['clip_id']:c for c in request['clips']}
        for s in result['scenes']:
            c=by_id[s['clip_id']]
            slots.append({'slot_id':'broll-job-'+job_id+'-'+s['id'],'file':s['file'],
                          'clip_id':s['clip_id'],'clip_idx':c['clip_idx'],'start':s['start'],'end':s['end'],
                          'binding':'source_interval','anchor':c['src_in']+s['start']-c['cut_start'],
                          'src_in':0,'speed':1,'kind':'free','shift':0,'_cov':False,
                          'name':'B-Roll '+s['id'],'geo':{'x':.5,'y':.5 if s['layout']=='fullscreen' else .2,'w':1},
                          'broll_job_id':job_id,'sha256':s['sha256']})
        ovr['broll']=slots
        if any(s['layout']=='overhead' for s in result['scenes']):
            # Per-clip framing avoids changing unrelated clips in range jobs.
            overhead={s['clip_id'] for s in result['scenes'] if s['layout']=='overhead'}
            for c in ovr['timeline_clips']:
                if c['id'] in overhead:
                    i=next(k for k,old in enumerate(ovr['timeline_clips']) if old['id']==c['id'])
                    body_shift=float(layout.get('video_yoff',0)) if 0<i<len(ovr['timeline_clips'])-1 else 0
                    c['oy']=(result['facecam_yoff']-body_shift)/1920*100
        audio=layout.setdefault('audio',{})
        audio['sfx_events']=list(audio.get('sfx_events') or [])+[dict(e,broll_job_id=job_id) for e in result['sfx_events']]
        # Existing global SFX is retained, but refuse possible duplicate overlays instead of guessing.
        if result['sfx_events'] and audio.get('sfx_file'):
            raise ValueError('Existing global SFX track requires explicit replacement before import')
        assert_snapshot_current(workdir,request)
        saved=sf_project.save_settings(workdir,{'revision':request['settings_revision'],'overrides':ovr,
                                              'layout':layout,'captions':current['captions']})
        atomic_json(directory/'imported.json',{'job_id':job_id,'snapshot_id':request['snapshot_id'],
                                              'revision':saved['revision']})
        return dict(saved,imported=True,duplicate=False,job_id=job_id)


def job_status(workdir, job_id=None):
    root=Path(workdir).resolve()/'broll_jobs'
    if job_id: directory=_job_directory(workdir,job_id)
    else:
        jobs=[(read_json(p),p.parent) for p in root.glob('*/job.json')]
        if not jobs: return None
        _,directory=max(jobs,key=lambda item:item[0].get('createdAt',0))
    doc=read_json(directory/'job.json')
    if not doc:return None
    if doc.get('status') in ('queued','running'):
        from cutter_broll_jobs import process_group_alive
        import time
        age=time.time()-doc.get('createdAt',time.time())
        if process_group_alive(directory,doc) is False and (doc['status']=='running' or age>120):
            return dict(doc,status='interrupted',error='B-Roll-Prozess wurde unterbrochen. Erneut starten ist möglich.')
    receipt=read_json(directory/'imported.json')
    if receipt:return dict(doc,status='imported',imported=receipt)
    if doc.get('status')=='done' and doc.get('schema_version')==2:
        try: assert_snapshot_current(workdir,read_json(directory/'auftrag.json'))
        except (sf_project.RevisionConflict,OSError) as error:return dict(doc,status='conflict',error=str(error))
    return doc
