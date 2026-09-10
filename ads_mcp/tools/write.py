"""Ferramentas de escrita (mutate) para o fork do Google Ads MCP.

As ferramentas executam as alterações imediatamente. A aprovação humana
antes de cada alteração é responsabilidade das instruções do agente.
"""

import re
from typing import Any, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers
from google.protobuf import json_format
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

write_mcp = FastMCP("write")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

MatchType = Literal["EXACT", "PHRASE", "BROAD"]
_MAX_KEYWORDS = 200


def _service(client, name: str):
    return client.get_service(name, interceptors=[MCPHeaderInterceptor()])


def _raise(ex: GoogleAdsException):
    msgs = [f"Google Ads API Error: {e.message}" for e in ex.failure.errors]
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(msgs))


def _log(tool: str, customer_id: str, detail: Dict[str, Any]):
    utils.logger.info(f"ads_mcp.write {tool} customer={customer_id} {detail}")


def _send(mutate_fn, req):
    try:
        return mutate_fn(request=req)
    except GoogleAdsException as ex:
        _raise(ex)


def _request(client, type_name: str, customer_id: str):
    req = client.get_type(type_name)
    req.customer_id = customer_id
    req.partial_failure = False
    return req


def _clean_keywords(keywords: List[str]) -> List[str]:
    cleaned = []
    for k in keywords:
        k = " ".join(k.split())
        if k and k not in cleaned:
            cleaned.append(k)
    if not cleaned:
        raise ToolError("Nenhuma palavra-chave válida informada.")
    return cleaned


def _result(tool, customer_id, detail):
    _log(tool, customer_id, detail)
    return {"applied": True, **detail}


# ---------------------------------------------------------------- campanhas


@write_mcp.tool(annotations=_WRITE)
def set_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: Literal["PAUSED", "ENABLED"],
) -> Dict[str, Any]:
    """Pausa ou ativa uma campanha.

    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "CampaignService")

    op = client.get_type("CampaignOperation")
    campaign = op.update
    campaign.resource_name = svc.campaign_path(customer_id, campaign_id)
    campaign.status = client.enums.CampaignStatusEnum[status]
    client.copy_from(
        op.update_mask, protobuf_helpers.field_mask(None, campaign._pb)
    )

    req = _request(client, "MutateCampaignsRequest", customer_id)
    req.operations.append(op)
    _send(svc.mutate_campaigns, req)

    return _result(
        "set_campaign_status", customer_id,
        {"campaign_id": campaign_id, "new_status": status},
    )


@write_mcp.tool(annotations=_WRITE)
def update_campaign_budget(
    customer_id: str,
    campaign_id: str,
    new_daily_budget: float,
    max_change_pct: float = 30.0,
) -> Dict[str, Any]:
    """Altera o orçamento diário de uma campanha (valor na moeda da conta).

    Recusa variações acima de max_change_pct em relação ao orçamento atual
    e orçamentos compartilhados (afetariam outras campanhas).
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    ga = _service(client, "GoogleAdsService")

    query = (
        "SELECT campaign.campaign_budget, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared FROM campaign "
        f"WHERE campaign.id = {int(campaign_id)}"
    )
    try:
        rows = list(ga.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        _raise(ex)
    if not rows:
        raise ToolError(f"Campanha {campaign_id} não encontrada.")

    row = rows[0]
    if row.campaign_budget.explicitly_shared:
        raise ToolError("Orçamento compartilhado: altere manualmente na interface.")

    current = row.campaign_budget.amount_micros / 1_000_000
    change_pct = abs(new_daily_budget - current) / current * 100 if current else 100
    if change_pct > max_change_pct:
        raise ToolError(
            f"Variação de {change_pct:.1f}% excede o limite de "
            f"{max_change_pct:.0f}% (atual: {current:.2f})."
        )

    svc = _service(client, "CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    budget = op.update
    budget.resource_name = row.campaign.campaign_budget
    budget.amount_micros = int(round(new_daily_budget * 1_000_000))
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, budget._pb))

    req = _request(client, "MutateCampaignBudgetsRequest", customer_id)
    req.operations.append(op)
    _send(svc.mutate_campaign_budgets, req)

    return _result(
        "update_campaign_budget", customer_id,
        {
            "campaign_id": campaign_id,
            "budget_before": current,
            "budget_after": new_daily_budget,
            "change_pct": round(change_pct, 1),
        },
    )


# ----------------------------------------------------------- palavras-chave


@write_mcp.tool(annotations=_WRITE)
def add_campaign_negative_keywords(
    customer_id: str,
    campaign_id: str,
    keywords: List[str],
    match_type: MatchType = "PHRASE",
) -> Dict[str, Any]:
    """Adiciona palavras-chave negativas no nível da CAMPANHA.

    Cada palavra consome 1 operação da cota diária. Máximo de 200 por
    chamada.
    """
    keywords = _clean_keywords(keywords)
    if len(keywords) > _MAX_KEYWORDS:
        raise ToolError(f"Máximo de {_MAX_KEYWORDS} negativas por chamada.")

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "CampaignCriterionService")
    campaign_rn = svc.campaign_path(customer_id, campaign_id)

    req = _request(client, "MutateCampaignCriteriaRequest", customer_id)
    for text in keywords:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_rn
        crit.negative = True
        crit.keyword.text = text
        crit.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
        req.operations.append(op)
    _send(svc.mutate_campaign_criteria, req)

    return _result(
        "add_campaign_negative_keywords", customer_id,
        {
            "campaign_id": campaign_id,
            "match_type": match_type,
            "count": len(keywords),
            "keywords": keywords,
        },
    )


@write_mcp.tool(annotations=_WRITE)
def add_ad_group_negative_keywords(
    customer_id: str,
    ad_group_id: str,
    keywords: List[str],
    match_types: List[MatchType] = ["PHRASE", "EXACT"],
) -> Dict[str, Any]:
    """Adiciona palavras-chave negativas no nível do GRUPO DE ANÚNCIOS.

    Por padrão cria cada termo em Frase E Exata (2 negativas por termo).
    Cada negativa criada consome 1 operação da cota diária (termos x
    tipos de correspondência). Máximo de 200 negativas por chamada.
    """
    keywords = _clean_keywords(keywords)
    match_types = list(dict.fromkeys(match_types))
    if not match_types:
        raise ToolError("Informe ao menos um tipo de correspondência.")
    total = len(keywords) * len(match_types)
    if total > _MAX_KEYWORDS:
        raise ToolError(
            f"{total} negativas nesta chamada; o máximo é {_MAX_KEYWORDS}. "
            "Divida em chamadas menores."
        )

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "AdGroupCriterionService")
    ad_group_rn = svc.ad_group_path(customer_id, ad_group_id)

    req = _request(client, "MutateAdGroupCriteriaRequest", customer_id)
    for text in keywords:
        for mt in match_types:
            op = client.get_type("AdGroupCriterionOperation")
            crit = op.create
            crit.ad_group = ad_group_rn
            crit.negative = True
            crit.keyword.text = text
            crit.keyword.match_type = client.enums.KeywordMatchTypeEnum[mt]
            req.operations.append(op)
    _send(svc.mutate_ad_group_criteria, req)

    return _result(
        "add_ad_group_negative_keywords", customer_id,
        {
            "ad_group_id": ad_group_id,
            "match_types": match_types,
            "terms": len(keywords),
            "operations": total,
            "keywords": keywords,
        },
    )


@write_mcp.tool(annotations=_WRITE)
def add_ad_group_keywords(
    customer_id: str,
    ad_group_id: str,
    keywords: List[str],
    match_type: MatchType = "PHRASE",
    cpc_bid: float | None = None,
) -> Dict[str, Any]:
    """Adiciona palavras-chave POSITIVAS (ativas) a um grupo de anúncios.

    cpc_bid é opcional (moeda da conta); deixe vazio em campanhas com
    lance automático. Cada palavra consome 1 operação. Máximo de 200 por
    chamada.
    """
    keywords = _clean_keywords(keywords)
    if len(keywords) > _MAX_KEYWORDS:
        raise ToolError(f"Máximo de {_MAX_KEYWORDS} palavras por chamada.")

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "AdGroupCriterionService")
    ad_group_rn = svc.ad_group_path(customer_id, ad_group_id)

    req = _request(client, "MutateAdGroupCriteriaRequest", customer_id)
    for text in keywords:
        op = client.get_type("AdGroupCriterionOperation")
        crit = op.create
        crit.ad_group = ad_group_rn
        crit.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        crit.keyword.text = text
        crit.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
        if cpc_bid:
            crit.cpc_bid_micros = int(round(cpc_bid * 1_000_000))
        req.operations.append(op)
    _send(svc.mutate_ad_group_criteria, req)

    return _result(
        "add_ad_group_keywords", customer_id,
        {
            "ad_group_id": ad_group_id,
            "match_type": match_type,
            "cpc_bid": cpc_bid,
            "count": len(keywords),
            "keywords": keywords,
        },
    )


@write_mcp.tool(annotations=_WRITE)
def remove_keywords(
    customer_id: str,
    level: Literal["campaign", "ad_group"],
    parent_id: str,
    criterion_ids: List[str],
) -> Dict[str, Any]:
    """Remove palavras-chave (positivas ou negativas) de uma campanha ou
    grupo de anúncios.

    level: "campaign" (parent_id = id da campanha) ou "ad_group"
    (parent_id = id do grupo). Os criterion_ids vêm de uma consulta em
    campaign_criterion ou ad_group_criterion. Só remove critérios do tipo
    KEYWORD; recusa qualquer outro (localização, público etc.).
    Remoção não pode ser desfeita (só recriando).
    """
    ids = list(dict.fromkeys(str(int(i)) for i in criterion_ids))
    if not ids:
        raise ToolError("Nenhum criterion_id informado.")
    if len(ids) > _MAX_KEYWORDS:
        raise ToolError(f"Máximo de {_MAX_KEYWORDS} remoções por chamada.")

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    ga = _service(client, "GoogleAdsService")

    if level == "campaign":
        res, parent_field = "campaign_criterion", "campaign.id"
    else:
        res, parent_field = "ad_group_criterion", "ad_group.id"
    query = (
        f"SELECT {res}.criterion_id, {res}.type, {res}.negative, "
        f"{res}.keyword.text, {res}.keyword.match_type FROM {res} "
        f"WHERE {parent_field} = {int(parent_id)} "
        f"AND {res}.criterion_id IN ({','.join(ids)})"
    )
    try:
        rows = list(ga.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        _raise(ex)

    found = {}
    for row in rows:
        crit = getattr(row, res)
        found[str(crit.criterion_id)] = crit
    missing = [i for i in ids if i not in found]
    if missing:
        raise ToolError(f"Critérios não encontrados neste {level}: {missing}")
    not_kw = [i for i, c in found.items() if c.type_.name != "KEYWORD"]
    if not_kw:
        raise ToolError(f"Critérios que não são palavra-chave (recusados): {not_kw}")

    if level == "campaign":
        svc = _service(client, "CampaignCriterionService")
        req = _request(client, "MutateCampaignCriteriaRequest", customer_id)
        for i in ids:
            op = client.get_type("CampaignCriterionOperation")
            op.remove = svc.campaign_criterion_path(customer_id, parent_id, i)
            req.operations.append(op)
        _send(svc.mutate_campaign_criteria, req)
    else:
        svc = _service(client, "AdGroupCriterionService")
        req = _request(client, "MutateAdGroupCriteriaRequest", customer_id)
        for i in ids:
            op = client.get_type("AdGroupCriterionOperation")
            op.remove = svc.ad_group_criterion_path(customer_id, parent_id, i)
            req.operations.append(op)
        _send(svc.mutate_ad_group_criteria, req)

    removed = [
        {
            "criterion_id": i,
            "text": found[i].keyword.text,
            "match_type": found[i].keyword.match_type.name,
            "negative": bool(found[i].negative),
        }
        for i in ids
    ]
    return _result(
        "remove_keywords", customer_id,
        {"level": level, "parent_id": parent_id, "count": len(ids), "removed": removed},
    )


# ----------------------------------------------------------------- anúncios


@write_mcp.tool(annotations=_WRITE)
def create_responsive_search_ad(
    customer_id: str,
    ad_group_id: str,
    headlines: List[str],
    descriptions: List[str],
    final_url: str,
    path1: str = "",
    path2: str = "",
) -> Dict[str, Any]:
    """Cria um anúncio responsivo de pesquisa (RSA), sempre PAUSADO.

    headlines: 3 a 15 títulos, até 30 caracteres cada.
    descriptions: 2 a 4 descrições, até 90 caracteres cada.
    path1/path2: até 15 caracteres cada (path2 exige path1).
    Para "editar" um RSA: crie o novo com esta ferramenta e pause o antigo
    com set_ad_status.
    """
    headlines = [" ".join(h.split()) for h in headlines if h.strip()]
    descriptions = [" ".join(d.split()) for d in descriptions if d.strip()]

    problems = []
    if not 3 <= len(headlines) <= 15:
        problems.append(f"{len(headlines)} títulos (precisa de 3 a 15)")
    if not 2 <= len(descriptions) <= 4:
        problems.append(f"{len(descriptions)} descrições (precisa de 2 a 4)")
    problems += [f"título com {len(h)} caracteres: '{h}'" for h in headlines if len(h) > 30]
    problems += [f"descrição com {len(d)} caracteres: '{d}'" for d in descriptions if len(d) > 90]
    if len(set(h.lower() for h in headlines)) < len(headlines):
        problems.append("títulos repetidos")
    if len(path1) > 15 or len(path2) > 15:
        problems.append("path1/path2 acima de 15 caracteres")
    if path2 and not path1:
        problems.append("path2 preenchido sem path1")
    if not final_url.startswith(("http://", "https://")):
        problems.append("final_url precisa começar com http:// ou https://")
    if problems:
        raise ToolError("Anúncio recusado: " + "; ".join(problems))

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "AdGroupAdService")

    op = client.get_type("AdGroupAdOperation")
    aga = op.create
    aga.ad_group = svc.ad_group_path(customer_id, ad_group_id)
    aga.status = client.enums.AdGroupAdStatusEnum.PAUSED
    aga.ad.final_urls.append(final_url)
    rsa = aga.ad.responsive_search_ad
    for h in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = h
        rsa.headlines.append(asset)
    for d in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = d
        rsa.descriptions.append(asset)
    if path1:
        rsa.path1 = path1
    if path2:
        rsa.path2 = path2

    req = _request(client, "MutateAdGroupAdsRequest", customer_id)
    req.operations.append(op)
    resp = _send(svc.mutate_ad_group_ads, req)

    detail = {
        "ad_group_id": ad_group_id,
        "status": "PAUSED",
        "headlines": headlines,
        "descriptions": descriptions,
        "final_url": final_url,
        "path1": path1,
        "path2": path2,
    }
    if resp and resp.results:
        detail["resource_name"] = resp.results[0].resource_name
    return _result("create_responsive_search_ad", customer_id, detail)


@write_mcp.tool(annotations=_WRITE)
def set_ad_status(
    customer_id: str,
    ad_group_id: str,
    ad_id: str,
    status: Literal["PAUSED", "ENABLED"],
) -> Dict[str, Any]:
    """Pausa ou ativa um anúncio específico dentro de um grupo.

    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "AdGroupAdService")

    op = client.get_type("AdGroupAdOperation")
    aga = op.update
    aga.resource_name = svc.ad_group_ad_path(customer_id, ad_group_id, ad_id)
    aga.status = client.enums.AdGroupAdStatusEnum[status]
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, aga._pb))

    req = _request(client, "MutateAdGroupAdsRequest", customer_id)
    req.operations.append(op)
    _send(svc.mutate_ad_group_ads, req)

    return _result(
        "set_ad_status", customer_id,
        {"ad_group_id": ad_group_id, "ad_id": ad_id, "new_status": status},
    )


# ------------------------------------------------------- acesso genérico
#
# As ferramentas abaixo liberam o restante da API. Use as ferramentas
# específicas acima sempre que existirem: elas validam mais coisas.

_MAX_GENERIC_OPS = 200

# Recursos cuja remoção apaga estrutura inteira ou afeta várias campanhas.
_STRUCTURAL = {
    "campaign_operation",
    "ad_group_operation",
    "campaign_budget_operation",
    "asset_group_operation",
    "bidding_strategy_operation",
    "conversion_action_operation",
    "shared_set_operation",
    "user_list_operation",
    "label_operation",
    "experiment_operation",
    "campaign_draft_operation",
}

# Serviços bloqueados no nível de acesso Explorer (exigem Basic ou Standard).
_EXPLORER_BLOCKED = {
    "CustomerUserAccessInvitationService",
    "CustomerUserAccessService",
    "KeywordPlanService",
    "KeywordPlanIdeaService",
    "KeywordPlanCampaignService",
    "KeywordPlanCampaignKeywordService",
    "KeywordPlanAdGroupService",
    "KeywordPlanAdGroupKeywordService",
    "AudienceInsightsService",
    "ReachPlanService",
    "PaymentsAccountService",
    "BillingSetupService",
    "AccountBudgetProposalService",
    "InvoiceService",
}
_EXPLORER_BLOCKED_METHODS = {("CustomerService", "CreateCustomerClient")}

_READ_PREFIXES = ("Get", "List", "Search", "Suggest", "Generate")


def _to_dict(message) -> Dict[str, Any]:
    pb = getattr(message, "_pb", message)
    return json_format.MessageToDict(pb, preserving_proto_field_name=True)


def _camel(name: str) -> str:
    if "_" in name or name[:1].islower():
        return "".join(p.capitalize() for p in name.split("_"))
    return name


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@write_mcp.tool(annotations=_WRITE)
def mutate(
    customer_id: str,
    operations: List[Dict[str, Any]],
    allow_structural_remove: bool = False,
) -> Dict[str, Any]:
    """Executa qualquer alteração via GoogleAdsService.Mutate (64 tipos de
    recurso: campanhas, grupos, anúncios, critérios, assets, extensões,
    lances, conversões, públicos, labels, listas compartilhadas etc.).

    Prefira as ferramentas específicas (write_add_ad_group_negative_keywords,
    write_create_responsive_search_ad etc.) quando existirem.

    operations: lista de MutateOperation em JSON, com nomes de campo em
    snake_case e enums como texto. Exemplos:
      {"ad_group_criterion_operation": {"create": {
          "ad_group": "customers/123/adGroups/456", "negative": true,
          "keyword": {"text": "grátis", "match_type": "PHRASE"}}}}
      {"campaign_operation": {"update": {
          "resource_name": "customers/123/campaigns/789",
          "status": "PAUSED"}}}
      {"ad_group_criterion_operation": {
          "remove": "customers/123/adGroupCriteria/456~111"}}
    Em "update", o update_mask é calculado sozinho se não for informado.

    Todas as operações são atômicas: ou todas passam, ou nenhuma. Máximo
    de 200 por chamada, e cada uma consome 1 operação da cota diária.
    Remover campanha, grupo, orçamento, estratégia de lance, conversão,
    lista compartilhada, público, label ou experimento exige
    allow_structural_remove=True, que só deve ser usado depois de o
    usuário aprovar essa remoção especificamente.
    """
    if not operations:
        raise ToolError("Nenhuma operação informada.")
    if len(operations) > _MAX_GENERIC_OPS:
        raise ToolError(f"Máximo de {_MAX_GENERIC_OPS} operações por chamada.")

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    svc = _service(client, "GoogleAdsService")

    req = _request(client, "MutateGoogleAdsRequest", customer_id)
    summary: Dict[str, int] = {}
    for i, raw in enumerate(operations):
        op = client.get_type("MutateOperation")
        try:
            json_format.ParseDict(raw, op._pb)
        except json_format.ParseError as e:
            raise ToolError(f"Operação {i} inválida: {e}")

        kind = op._pb.WhichOneof("operation")
        if kind is None:
            raise ToolError(f"Operação {i} vazia ou com tipo desconhecido.")
        if kind.startswith("keyword_plan_"):
            raise ToolError("Keyword Planner não está liberado no acesso Explorer.")

        inner = getattr(op, kind)
        action = inner._pb.WhichOneof("operation")
        if action is None:
            raise ToolError(f"Operação {i} sem create, update ou remove.")
        if action == "remove" and kind in _STRUCTURAL and not allow_structural_remove:
            raise ToolError(
                f"Operação {i} remove um recurso estrutural ({kind}). "
                "Peça aprovação específica e use allow_structural_remove=True."
            )
        if action == "update" and not inner.update_mask.paths:
            client.copy_from(
                inner.update_mask,
                protobuf_helpers.field_mask(None, inner.update._pb),
            )

        key = f"{kind.removesuffix('_operation')}.{action}"
        summary[key] = summary.get(key, 0) + 1
        req.mutate_operations.append(op)

    resp = _send(svc.mutate, req)

    detail: Dict[str, Any] = {"operations": len(operations), "summary": summary}
    if resp is not None:
        names = []
        for r in resp.mutate_operation_responses:
            kind = r._pb.WhichOneof("response")
            if kind:
                names.append(getattr(r, kind).resource_name)
        detail["resource_names"] = names
    return _result("mutate", customer_id, detail)


@write_mcp.tool(annotations=_WRITE)
def call_service(
    customer_id: str,
    service: str,
    method: str,
    request: Dict[str, Any],
) -> Dict[str, Any]:
    """Chama qualquer outro método da API do Google Ads que não seja coberto
    por mutate ou search. Exemplos: RecommendationService.ApplyRecommendation
    e DismissRecommendation, ConversionUploadService.UploadClickConversions,
    ConversionAdjustmentUploadService, GeoTargetConstantService.
    SuggestGeoTargetConstants, AssetGenerationService.

    service: nome do serviço (ex.: "RecommendationService").
    method: nome do método (ex.: "ApplyRecommendation").
    request: corpo da requisição em JSON (snake_case); customer_id é
    preenchido automaticamente quando o método aceita.

    Métodos que alteram algo executam imediatamente.
    """
    service = _camel(service.removesuffix("Service")) + "Service"
    method_camel = _camel(method)

    if service == "GoogleAdsService":
        raise ToolError("Use search_search para consultas e write_mutate para alterações.")
    if service in _EXPLORER_BLOCKED or (service, method_camel) in _EXPLORER_BLOCKED_METHODS:
        raise ToolError(
            f"{service}.{method_camel} não está liberado no acesso Explorer "
            "(exige Basic ou Standard)."
        )

    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client()
    try:
        svc = _service(client, service)
        fn = getattr(svc, _snake(method_camel))
        req = client.get_type(f"{method_camel}Request")
    except (ValueError, AttributeError, KeyError) as e:
        raise ToolError(f"Serviço ou método não encontrado: {service}.{method_camel} ({e})")

    try:
        json_format.ParseDict(request or {}, req._pb)
    except json_format.ParseError as e:
        raise ToolError(f"Requisição inválida: {e}")

    fields = req._pb.DESCRIPTOR.fields_by_name
    if "customer_id" in fields and not req.customer_id:
        req.customer_id = customer_id

    is_read = method_camel.startswith(_READ_PREFIXES)
    detail: Dict[str, Any] = {"service": service, "method": method_camel}

    resp = _send(fn, req)
    if resp is not None and hasattr(resp, "_pb"):
        detail["response"] = _to_dict(resp)
    _log("call_service", customer_id, detail)
    return {"applied": not is_read, **detail}
