from fastapi import FastAPI

from app.routers import events, reconciliation, transactions

app = FastAPI(title="Payment Reconciliation Service")

app.include_router(events.router)
app.include_router(transactions.router)
app.include_router(reconciliation.router)
