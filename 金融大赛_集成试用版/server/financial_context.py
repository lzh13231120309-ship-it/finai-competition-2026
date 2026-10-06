"""Explicit user clarifications are separate from immutable document facts."""
import re
from . import db

def currency(task_id):
    current_files=[];confirmation=None
    for message in db.list_messages(task_id):
        if message['role']!='user':continue
        if message.get('attachments'):
            current_files=[x['name'] for x in message['attachments'] if not x['name'].lower().endswith('.json')]
            confirmation=None
        match=re.fullmatch(r'(?:是|币种(?:是|为)?|用|按)?(人民币|CNY|美元|USD|港币|HKD)[。.!！]?',(message.get('content') or '').strip(),re.I)
        if match and len(current_files)==1:
            label=match[1].upper();code={'人民币':'CNY','美元':'USD','港币':'HKD'}.get(label,label)
            confirmation={'currency':code,'filename':current_files[0],'message_id':message['id'],'origin':'user_confirmation','description':'用户明确确认本文件币种；不代表原文已披露'}
    return confirmation

def apply(records,source,confirmation):
    if not confirmation or confirmation['filename']!=source['filename']:return False
    changed=False
    for record in records:
        if record['source_ref']['filename']!=source['filename']:continue
        value=record.get('currency')
        if value in (None,'','未知','未明确'):
            record['currency']=confirmation['currency']
            record['review_reasons']=[r for r in record.get('review_reasons',[]) if r!='currency_unknown']
            record['source_ref']['user_confirmation']=confirmation
            changed=True
    return changed
