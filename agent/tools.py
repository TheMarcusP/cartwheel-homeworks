"""Homework 1: the remaining commerce-agent tools.

The three lecture tools (`search_help_center`, `get_order`, `issue_refund`)
are implemented in agent/agent.py and are worked examples of the pattern:
check permissions first, go through agent/db.py for data, and return a
structured dict, never a prose error. The homework tools follow the same
pattern. agent/agent.py already wraps each function below as an SDK tool, so
once a function works here it works in chat with no further wiring.

Result convention (see agent/auth.py):
  - Success: a dict with "ok": True plus the payload fields named in each
    docstring.
  - Failure: {"ok": False, "error": <code>, "reason": <human-readable str>}.

Run the contract tests with: uv run pytest tests/test_hw_holes.py -k hw1
They are marked xfail and flip to passing as you implement each function.
"""

from __future__ import annotations

from typing import Any

from agent import db
from agent.auth import AuthContext, can_cancel_order, permission_denied
from agent.helpcenter import load_policy_docs
from agent.killswitch import kill_switch

MAX_SEARCH_LIMIT = 25
DEFAULT_ORDER_LIMIT = 20


def get_policy(ctx: AuthContext, policy_id: str) -> dict[str, Any]:
    """Fetch one policy doc by its exact id. Risk tier: read.

    Every role may read every policy doc (the corpus is public help-center
    content), so this tool needs no permission check.

    Args:
        ctx: The caller's auth context. Unused here, but every tool takes it.
        policy_id: An exact policy id, e.g. "cw-returns" or
            "store-juniper-home-goods-policy". Matching is exact and
            case-sensitive; ids are the `policy_id` front-matter field of the
            files in data/policies/.

    Returns:
        On success: {"ok": True, "policy_id": str, "title": str,
        "audience": str, "body": str} where body is the markdown body of the
        doc without the front matter.
        If no doc has that id: {"ok": False, "error": "not_found",
        "reason": ...} naming the id that was requested.

    Implementation notes:
        agent.helpcenter.load_policy_docs() returns every parsed doc.
    """
    # Step 1: get a list of all 18 policy documents.
    docs = load_policy_docs()

    # Step 2: look at each document, one at a time.
    for doc in docs:
        # Is this the one the model asked for?
        if doc.policy_id == policy_id:
            # Yes: send back its label info and text.
            return {
                "ok": True,
                "policy_id": doc.policy_id,
                "title": doc.title,
                "audience": doc.audience,
                "body": doc.body,
            }

    # Step 3: we only reach here if the loop found no match.
    return {
        "ok": False,
        "error": "not_found",
        "reason": f"no policy with id {policy_id}",
    }


def search_products(
    ctx: AuthContext,
    query: str,
    store: str | None = None,
    max_price_usd: float | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search the product catalog. Risk tier: read.

    Every role may search products. Matching is deterministic keyword
    matching, not semantic search: a product matches when every whitespace
    token of `query` appears case-insensitively as a substring of the
    product's title or description.

    Args:
        ctx: The caller's auth context.
        query: Free-text query. Must be non-empty after stripping whitespace;
            otherwise return {"ok": False, "error": "invalid_argument",
            "reason": ...}.
        store: Optional store filter. Matched with
            agent.db.get_store_by_name (case-insensitive name or slug). If
            given and no store matches, return {"ok": False, "error":
            "not_found", "reason": ...} naming the store string.
        max_price_usd: Optional inclusive price ceiling. If given and not
            strictly positive, return an "invalid_argument" error.
        limit: Maximum products to return. Clamp to the range
            [1, MAX_SEARCH_LIMIT]; do not error on out-of-range values.

    Returns:
        {"ok": True, "products": [...], "count": <len(products)>} where each
        product is {"product_id": int, "store_id": int, "title": str,
        "price_usd": float}. Sort matches by price_usd ascending, then by
        product_id ascending, and truncate to `limit`. No matches is still a
        success: {"ok": True, "products": [], "count": 0}.

    Implementation notes:
        agent.db.list_products(conn, store_id) gives the candidate set.
        Use `with db.connection() as conn:` to close the database automatically.
    """
    # Step 1: validate inputs. Empty/whitespace-only query -> invalid_argument.
    #         max_price_usd given but not > 0 -> invalid_argument.
    if not query.strip():
        return {"ok": False, "error": "invalid_argument", "reason": "Query is empty; ask user what product they are looking for"}
    if max_price_usd is not None and max_price_usd <= 0: return {"ok": False, "error": "invalid_argument",
            "reason": f"max price msut be greater than 0; got {max_price_usd}; ask user for valid budget greater than 0"}

    # Step 2: clamp limit into [1, MAX_SEARCH_LIMIT].
    if limit < 1: limit = 1
    if limit > MAX_SEARCH_LIMIT: limit = MAX_SEARCH_LIMIT
    # Step 3: open the database. If `store` was given, look it up with
    #         db.get_store_by_name; no match -> not_found naming the store.
    #         Then get candidates with db.list_products(conn, store_id).
    with db.connection() as conn:
        store_id = None
        if store is not None:
            lookup_store = db.get_store_by_name(conn, store)
            if not lookup_store:
                return{"ok": False, "error": "not_found", "reason": f"No store named {store}; ask user to check store name is correct"}
            store_id =  lookup_store.id
        products = db.list_products(conn, store_id)
    # Step 4: keep a product only if EVERY word of the query appears
    #         (case-insensitive) in its title or description, and its
    #         price_usd is within max_price_usd (if given).
    words = query.lower().split()
    matches = []

    for product in products:
        text = (product.title + " " + product.description).lower()

        if not all(word in text for word in words):
            continue
        if  max_price_usd is not None and product.price_usd > max_price_usd:
            continue
        matches.append(product)
    # Step 5: sort by (price_usd, product id), cut to `limit`, convert each
    #         to {"product_id", "store_id", "title", "price_usd"}, return.
    sorted_products = sorted(matches, key=lambda p:(p.price_usd, p.id))
    limited_products = sorted_products[:limit]
    results = []
    for p in limited_products:
        results.append({"product_id": p.id, "store_id": p.store_id, "title": p.title, "price_usd": p.price_usd})
    return {"ok": True, "products": results, "count": len(results)}


def list_my_orders(ctx: AuthContext) -> dict[str, Any]:
    """List recent orders in the caller's own scope. Risk tier: read.

    Role behavior, straight from the access matrix in SPEC.md:
        - shopper: the caller's own orders.
        - merchant: the caller's store's orders (ctx.store_id).
        - support: support staff have no orders of their own and look up
          specific orders with get_order instead, so return {"ok": False,
          "error": "invalid_argument", "reason": ...} saying exactly that.

    Returns:
        For shopper and merchant: {"ok": True, "orders": [...],
        "count": <len(orders)>} where each order is
        agent.db.Order.to_public_dict() and the list holds at most
        DEFAULT_ORDER_LIMIT orders, newest first (agent.db.list_orders_for_user
        and list_orders_for_store already sort and limit this way).

    Implementation notes:
        No permission check is needed beyond the role dispatch, because the
        scope is baked into which query you run. That is the point of the
        tool: the model cannot ask for someone else's orders through it.
    """
    # Support staff have no orders of their own: refuse with an explanation.
    if ctx.role == "support":
        return {"ok": False, "error": "invalid_argument", "reason": f"{ctx.role} have no orders; use get_order with specific order ID"}

    # Open the database; it closes automatically when the block ends.
    with db.connection() as conn:
        if ctx.role == "shopper":
            orders = db.list_orders_for_user(conn, ctx.user_id)
        elif ctx.role == "merchant":
            orders = db.list_orders_for_store(conn, ctx.store_id)
        else:
            # Unknown role: refuse rather than guess.
            return {
                "ok": False,
                "error": "invalid_argument",
                "reason": f"unsupported role {ctx.role}",
            }

    # Turn each Order object into the plain dict the model is allowed to see.
    public_orders = [order.to_public_dict() for order in orders]
    return {"ok": True, "orders": public_orders, "count": len(public_orders)}


def cancel_order(ctx: AuthContext, order_id: int, reason: str) -> dict[str, Any]:
    """Cancel an order. Risk tier: write.

    This is the homework's write tool, and it must enforce two independent
    rules in this order:

    1. The access matrix (scope): use agent.auth.can_cancel_order. Shoppers
       may cancel only their own orders, merchants only their own store's
       orders, support any order. On failure return
       agent.auth.permission_denied(...) with a reason naming the role and
       the order id. Scope is checked before the status rule so that an
       out-of-scope caller learns nothing about the order's state.
    2. The pre-shipment rule (facts.yaml `cancel_cutoff`): only orders whose
       status is exactly "placed" can be cancelled, for every role. If the
       order is in scope but its status is not "placed", return
       {"ok": False, "error": "not_eligible", "reason": ...} that names the
       current status and states that orders can be cancelled only before
       shipment.

    Args:
        ctx: The caller's auth context.
        order_id: The order to cancel.
        reason: Free-text reason from the user; not validated.

    Returns:
        If no order has this id: {"ok": False, "error": "not_found",
        "reason": ...}.
        On success: {"ok": True, "order_id": order_id, "status": "cancelled"}
        after persisting the new status with agent.db.set_order_status.

    Implementation notes:
        Fetch with agent.db.get_order. Note the argument order of
        can_cancel_order(ctx, order_user_id, order_store_id).

    The Module 4 kill switch is checked first (before the scope and
    status rules and before your code), so that a paused write tool touches
    nothing. It is provided; the default ("off") returns None and falls
    through to your implementation.
    """
    paused = kill_switch("cancel_order")
    if paused is not None:
        return {"ok": False, "error": "paused", "reason": paused}
    # Step 1: open the database and fetch the order. No order -> not_found.
    with db.connection() as conn:
        order = db.get_order(conn,order_id)
        if order is None: return {"ok": False, "error": "not_found", "reason": f"order not found; order given {order_id}; please ask user to provide valid order number"}
        # Step 2: SCOPE. Is this caller allowed to cancel this order?
        #         No -> permission_denied (reason names role + order id ONLY).
        if not can_cancel_order(ctx, order.user_id, order.store_id):
            return permission_denied(f"user with role {ctx.role} is not authorised to cancel order {order_id} Tell user you cannot act on this order")

        # Step 3: STATUS. Is the order still "placed"?
        #         No -> not_eligible (reason names the current status and that
        #         orders can be cancelled only before shipment).
        if order.status != "placed":
            return {"ok": False, "error": "not_eligible", "reason": f"The status of the order {order_id} is {order.status} it is not eligible for cancellation becuase orders can only be cancelled before shipment; user may want to request a return or refund instead"}
        # Step 4: save the new status "cancelled", then return the success dict.
        db.set_order_status(conn, order_id, "cancelled")
    return {"ok": True, "order_id": order_id, "status": "cancelled"}


def find_order(ctx: AuthContext, query: str) -> dict[str, Any]:
    """Search the caller's orders by product name. Risk tier: read.

    Takes a natural-language query (e.g., "earmuffs I bought last week")
    and searches the authenticated user's orders for products whose name
    matches. Use fuzzy string matching (e.g., thefuzz.fuzz.partial_ratio
    or case-insensitive substring matching) to find orders whose product name is close to the
    query.

    Access rules: a shopper searches only the shopper's own orders, a
    merchant searches orders from the merchant's store, and support staff
    can search any orders. Use agent.db.list_order_search_candidates with
    user_id=ctx.user_id for shoppers, store_id=ctx.store_id for merchants,
    or all_orders=True only for support. Derive the scope from ctx, never
    from the query; reject unsupported roles or missing required identity.
    Use agent.db.list_products to map product IDs to product titles.

    The helper returns the complete authorised scope, newest first with
    order ID descending as the tie-breaker. Match product names first,
    preserve that order, then return at most five matches. Do not search
    only the 20 most recent orders. Convert matches with to_public_dict().

    Args:
        ctx: The caller's auth context.
        query: A natural-language description of the product.

    Returns:
        {"ok": True, "orders": [...]} with a list of matching orders
        (at most 5), each as the dict returned by agent.db. If no orders
        match, return {"ok": True, "orders": []}.
    """
    # Step 1: SCOPE from ctx (never from the query).
    #         shopper -> user_id=ctx.user_id; merchant -> store_id=ctx.store_id;
    #         support -> all_orders=True; anything else -> refuse.
    with db.connection() as conn:
        if ctx.role == "shopper":
            candidates = db.list_order_search_candidates(conn, user_id=ctx.user_id)
        elif ctx.role == "merchant":
            candidates = db.list_order_search_candidates(conn, store_id=ctx.store_id)
        elif ctx.role == "support":
            candidates = db.list_order_search_candidates(conn, all_orders=True)
        else:
            return permission_denied(f"role {ctx.role} is not allowed to search orders; tell user this can't be done here or escalate to a human")
    # Step 2: open the database. Get candidates with
    #         db.list_order_search_candidates(conn, <your scope>), and a
    #         product-id -> title lookup built from db.list_products(conn).
        products = db.list_products(conn)
        titles = {}

        for p in products:
            titles[p.id] = p.title

    # Step 3: go through candidates IN THE ORDER GIVEN. Keep an order if its
    #         product title matches the query (your matching rule). Stop once
    #         you have 5. Convert each kept order with to_public_dict().

    query_lower = query.lower()
    found = []

    for order in candidates:
        title = titles[order.product_id].lower()
        if query_lower in title:
            found.append(order.to_public_dict())
            if len(found) == 5:
                break

    # Step 4: return {"ok": True, "orders": [...]}  (no "count" key here).
    return {"ok": True, "orders": found}
