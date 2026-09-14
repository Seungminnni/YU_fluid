"""논문에서 숫자로 재현 가능한 배터리 발열 상관식만 제공한다.

현재 보유한 학위논문 PDF에는 최종 Fluent UDF 원본이 없다.  표 4.2의 4차
계수는 유효숫자가 부족해 Kelvin 원시 거듭제곱을 계산할 때 큰 상쇄오차를
만든다.  따라서 그 계수를 최종식으로 가장하지 않고, 논문 식 (4.1)에
명시된 1차 expanded correlation만 별도 ``provisional`` 모델로 제공한다.
"""

from __future__ import annotations

import math

from .errors import ProfileError


PENEKLIOGLU_FIRST_ORDER_COEFFICIENTS = {
    "c_rate": 1.086,
    "soc": -0.84,
    "temperature_k": -0.01486,
    "intercept": 4.41,
}
PENEKLIOGLU_C_RATE_RANGE = (0.5, 2.0)
PENEKLIOGLU_SOC_RANGE = (0.0, 1.0)
PENEKLIOGLU_TEMPERATURE_RANGE_K = (293.15, 303.15)
PENEKLIOGLU_TRAINING_OUTPUT_MINIMUM_W = 0.5


def peneklioglu_expanded_first_order_q_w(
    c_rate: float, soc: float, temperature_k: float
) -> float:
    """논문 식 (4.1)의 셀당 발열 ``q(D,S,T)`` [W]를 계산한다."""

    values = (c_rate, soc, temperature_k)
    if not all(math.isfinite(value) for value in values):
        raise ProfileError("battery correlation inputs must be finite")
    if not PENEKLIOGLU_C_RATE_RANGE[0] <= c_rate <= PENEKLIOGLU_C_RATE_RANGE[1]:
        raise ProfileError("paper correlation requires discharge C-rate in [0.5, 2.0]")
    if not PENEKLIOGLU_SOC_RANGE[0] <= soc <= PENEKLIOGLU_SOC_RANGE[1]:
        raise ProfileError("paper correlation requires SOC in [0, 1]")
    if not (
        PENEKLIOGLU_TEMPERATURE_RANGE_K[0]
        <= temperature_k
        <= PENEKLIOGLU_TEMPERATURE_RANGE_K[1]
    ):
        raise ProfileError("paper correlation requires temperature in [293.15, 303.15] K")
    coefficients = PENEKLIOGLU_FIRST_ORDER_COEFFICIENTS
    return (
        coefficients["c_rate"] * c_rate
        + coefficients["soc"] * soc
        + coefficients["temperature_k"] * temperature_k
        + coefficients["intercept"]
    )


def discharge_soc(
    time_s: float,
    *,
    c_rate: float,
    initial_soc: float,
    outside_policy: str,
) -> float:
    """``SOC=S0-D*t/3600``을 계산하고 범위 밖 정책을 명시적으로 적용한다."""

    if not math.isfinite(time_s) or time_s < 0:
        raise ProfileError("evaluation time must be finite and nonnegative")
    raw_soc = initial_soc - c_rate * time_s / 3600.0
    if PENEKLIOGLU_SOC_RANGE[0] <= raw_soc <= PENEKLIOGLU_SOC_RANGE[1]:
        return raw_soc
    if outside_policy == "hold":
        return min(PENEKLIOGLU_SOC_RANGE[1], max(PENEKLIOGLU_SOC_RANGE[0], raw_soc))
    raise ProfileError(
        f"time {time_s:g} s gives SOC={raw_soc:.8g}, outside the paper range [0, 1]"
    )
