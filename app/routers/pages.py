"""Server-rendered pages.

Deliberately server-rendered: a procurement analyst needs a fast table, not a
client-side bundle. The pages read the same query layer as the API, so the UI can
never show something the API would refuse to return.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..intelligence.aggregates import product_price_stats
from ..intelligence.matching import comparable_set
from ..models import (
    Award,
    FinancialBid,
    Organisation,
    PriceObservation,
    Product,
    ProductCategory,
    Source,
    Supplier,
    Tender,
)
from ..llm_config import list_models, suggest_models, validate_key
from ..registry import check
from ..settings_store import SettingsStore
from .api import tender_detail

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _inr(value) -> str:
    """Indian-format currency for display: 1,94,120 rather than 194,120."""
    if value in (None, ""):
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    negative = amount < 0
    amount = abs(amount)
    whole = int(amount)
    frac = round(amount - whole, 2)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts) + "," + tail
    text = f"₹{digits}"
    if frac:
        text += f".{int(round(frac * 100)):02d}"
    return ("-" if negative else "") + text


def _shortdate(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        return value[:10]
    try:
        return value.strftime("%d %b %Y")
    except AttributeError:
        return str(value)


TEMPLATES.env.filters["inr"] = _inr
TEMPLATES.env.filters["shortdate"] = _shortdate
router = APIRouter(tags=["pages"])


def _ctx(request: Request, **kwargs) -> dict:
    return {"request": request, **kwargs}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    total_tenders = db.query(Tender).count()
    with_ladder = db.query(FinancialBid.tender_id).distinct().count()
    by_state = [
        {"state": state or "Unknown", "count": count}
        for state, count in db.query(Tender.state, func.count(Tender.id))
        .group_by(Tender.state).order_by(func.count(Tender.id).desc()).limit(8).all()
    ]
    by_basis = [
        {"basis": basis, "count": count}
        for basis, count in db.query(PriceObservation.basis, func.count(PriceObservation.id))
        .group_by(PriceObservation.basis).all()
    ]
    recent = list(db.scalars(select(Tender).order_by(Tender.id.desc()).limit(8)))
    sources = list(db.scalars(select(Source).order_by(Source.key)))
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            stats={
                "tenders": total_tenders,
                "with_ladder": with_ladder,
                "ladder_pct": round(with_ladder / total_tenders * 100, 1) if total_tenders else 0,
                "awards": db.query(Award).count(),
                "observations": db.query(PriceObservation).count(),
                "products": db.query(Product).count(),
                "suppliers": db.query(Supplier).count(),
                "buyers": db.query(Organisation).count(),
                "runnable_sources": sum(1 for s in sources if check(s).ok),
                "total_sources": len(sources),
            },
            by_state=by_state,
            by_basis=by_basis,
            recent=recent,
            sources=sources,
            llm=SettingsStore(db).public_view(),
        ),
    )


@router.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str | None = None,
    state: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    per_page = 20
    stmt = select(Tender)
    if q:
        terms = [t for t in q.split() if t]
        if terms:
            stmt = stmt.where(or_(*[Tender.title.ilike(f"%{t}%") for t in terms]))
    if state:
        stmt = stmt.where(Tender.state == state)
    if status:
        stmt = stmt.where(Tender.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.order_by(Tender.published_at.desc().nulls_last(), Tender.id.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
        )
    )
    states = [s for (s,) in db.query(Tender.state).distinct().order_by(Tender.state).all() if s]
    return TEMPLATES.TemplateResponse(
        request,
        "search.html",
        _ctx(
            request,
            q=q or "",
            state=state or "",
            status=status or "",
            total=total,
            page=page,
            per_page=per_page,
            pages=max(1, -(-total // per_page)),
            results=rows,
            states=states,
        ),
    )


@router.get("/tender/{tender_id}", response_class=HTMLResponse)
def tender_page(tender_id: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(Tender, tender_id)
    if t is None:
        return TEMPLATES.TemplateResponse(request, "not_found.html", _ctx(request, what="Tender"), status_code=404)
    return TEMPLATES.TemplateResponse(request, "tender.html", _ctx(request, t=tender_detail(db, t)))


@router.get("/prices", response_class=HTMLResponse)
def prices(
    request: Request,
    product_id: int | None = None,
    basis: str = "awarded",
    db: Session = Depends(get_db),
):
    products = list(
        db.scalars(
            select(Product)
            .join(PriceObservation, PriceObservation.product_id == Product.id)
            .group_by(Product.id)
            .order_by(func.count(PriceObservation.id).desc())
            .limit(40)
        )
    )
    selected = db.get(Product, product_id) if product_id else (products[0] if products else None)
    stats = product_price_stats(db, selected.id, basis=basis) if selected else None
    comparables = comparable_set(db, selected.id) if selected else None
    return TEMPLATES.TemplateResponse(
        request,
        "prices.html",
        _ctx(
            request,
            products=products,
            selected=selected,
            stats=stats,
            comparables=comparables,
            basis=basis,
        ),
    )


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, db: Session = Depends(get_db)):
    return TEMPLATES.TemplateResponse(request, "assistant.html", _ctx(request, llm=SettingsStore(db).public_view()))


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: Session = Depends(get_db)):
    rows = []
    for s in db.scalars(select(Source).order_by(Source.tier, Source.key)):
        gate = check(s)
        rows.append({"source": s, "gate": gate})
    return TEMPLATES.TemplateResponse(request, "sources.html", _ctx(request, rows=rows))


@router.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request, db: Session = Depends(get_db)):
    cats = list(db.scalars(select(ProductCategory).order_by(ProductCategory.path)))
    counts = {
        cid: count
        for cid, count in db.query(Tender.category_id, func.count(Tender.id)).group_by(Tender.category_id).all()
    }
    return TEMPLATES.TemplateResponse(
        request,
        "categories.html", _ctx(request, categories=cats, counts=counts)
    )


# --------------------------------------------------------------------------- #
# Settings — the "paste a key and it works" screen
# --------------------------------------------------------------------------- #
@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    message: str | None = None,
    tone: str | None = None,
):
    store = SettingsStore(db)
    runtime = store.llm()
    catalogue = list_models(runtime.base_url, runtime.api_key, timeout=5.0)
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        _ctx(
            request,
            llm=store.public_view(),
            models=catalogue,
            suggested=suggest_models(catalogue),
            message=message,
            tone=tone,
        ),
    )


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    db: Session = Depends(get_db),
    api_key: str = Form(""),
    model_extraction: str = Form(""),
    model_assistant: str = Form(""),
    action: str = Form("save"),
    force: str = Form(""),
):
    store = SettingsStore(db)
    runtime = store.llm()
    catalogue = list_models(runtime.base_url, runtime.api_key, timeout=5.0)

    def render(message: str, tone: str = "info"):
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                llm=store.public_view(),
                models=catalogue,
                suggested=suggest_models(catalogue),
                message=message,
                tone=tone,
            ),
        )

    if action == "clear":
        result = store.clear_llm(actor="ui")
        if result.configured:
            return render("Stored key removed. The key set in the environment is still in use.", "info")
        return render("API key removed. Extraction falls back to deterministic rules.", "ok")

    candidate = api_key.strip() or runtime.api_key

    if action == "test":
        if not candidate:
            return render("Paste a key first, then test it.", "warn")
        check = validate_key(candidate, runtime.base_url)
        return render(check.message, "ok" if check.ok else "error")

    # action == save
    verified = False
    check_message = None
    if candidate:
        check = validate_key(candidate, runtime.base_url)
        check_message = check.message
        verified = check.ok
        if not check.ok and not force:
            return render(
                f"{check.message} Nothing was saved — fix the key, or use "
                f"\"Save without verifying\" if this server cannot reach the provider.",
                "error",
            )

    store.save_llm(
        api_key=api_key.strip() or None,
        model_extraction=model_extraction or None,
        model_assistant=model_assistant or None,
        actor="ui",
        verified=verified,
    )
    if not candidate:
        return render("Models updated. No API key is configured yet.", "info")
    if verified:
        return render(f"Connected. {check_message}", "ok")
    return render(
        "Saved without verification — the provider was not reachable from this server. "
        "The key will be used on the next request.",
        "warn",
    )
