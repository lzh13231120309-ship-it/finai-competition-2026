"""Local-only readiness inspection; no inference requests and no credential output."""
import ipaddress
from urllib.parse import urlparse
from . import config,knowledge,providers


def status():
    cfg=config.load();provider=cfg.get('provider') or providers.DEFAULT_PROVIDER
    override=(cfg.get('base_url_overrides') or {}).get(provider)
    info=providers.PROVIDERS.get(provider,{})
    endpoint=override or info.get('base_url') or ''
    host=urlparse(endpoint).hostname or ''
    scope='public_network'
    if not host:scope='unconfigured'
    elif host.lower()=='localhost':scope='loopback'
    else:
        try:
            address=ipaddress.ip_address(host)
            if address.is_loopback:scope='loopback'
            elif address.is_private:scope='private_lan'
        except ValueError:
            if override and host != (urlparse(info.get("base_url") or "").hostname or ""):
                scope="hostname_unverified"
    bases=[item for item in knowledge.list_kbs() if item.get('enabled') and item.get('indexed')]
    return {'ok':True,'knowledge_retrieval_offline':True,'indexed_knowledge_bases':len(bases),
        'indexed_chunks':sum(item.get('chunks',0) for item in bases),'selected_provider':provider,
        'selected_model':cfg.get('model'),'endpoint_scope':scope,'inference_service_probed':False,
        'full_agent_offline_verified':False,
        'notes':['当前模型接口仍指向公网；断外网后不能据此完成聊天推理。请提前准备本地或经确认的局域网模型。' if scope=='public_network' else '当前接口未经过断外网演练；地址分类不能证明服务及模型权重可用，仍需实际部署验收。',
            '本地知识、规则计算与模型推理是不同依赖；本检查不发送网络请求，不输出密钥。']}
