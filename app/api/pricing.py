from fastapi import APIRouter

from app.api.schemas import PricingRequest, PricingResponse
from app.services.pricing_calculator import PricingCalculator

router = APIRouter(prefix="/pricing", tags=["Pricing"])
calculator = PricingCalculator()


@router.post("/calculate", response_model=PricingResponse)
async def calculate_price(request: PricingRequest):
    result = calculator.calculate(
        product_type=request.product_type,
        quantity=request.quantity,
        size_mm=request.size_mm,
        urgent=request.urgent,
    )
    return PricingResponse(**result)
