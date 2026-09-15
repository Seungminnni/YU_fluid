"""논문에서 숫자로 재현 가능한 배터리 발열 상관식만 제공한다.

현재 보유한 학위논문 PDF에는 최종 Fluent UDF 원본이 없다.  표 4.2의 4차
계수는 유효숫자가 부족해 Kelvin 원시 거듭제곱을 계산할 때 큰 상쇄오차를
만든다.  따라서 그 계수를 최종식으로 가장하지 않고, 논문 식 (4.1)에
명시된 1차 expanded correlation과 논문 그림에 공개된 2C·293.15 K의
내부저항/엔트로피 피팅식을 서로 다른 모델로 제공한다.
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

# 사용자 제공 ``UDF 설명_20260910 (2).pptx``가 선택한 재현 가능 조건이다.
# Ri는 학위논문 Figure 4.2의 Group 7(2C, 293.15 K), ΔS는 Figure 4.6의
# 293.15 K 곡선이다. 이 계수를 다른 방전율·온도에 외삽하지 않는다.
PENEKLIOGLU_BERNARDI_C_RATE = 2.0
PENEKLIOGLU_BERNARDI_CAPACITY_AH = 2.0
PENEKLIOGLU_BERNARDI_TEMPERATURE_K = 293.15
PENEKLIOGLU_FARADAY_C_MOL = 96485.33212
PENEKLIOGLU_RESISTANCE_COEFFICIENTS = {
    "constant": 0.0994,
    "soc": -0.145,
    "soc2": 0.246,
    "soc3": -0.126,
}
PENEKLIOGLU_ENTROPY_COEFFICIENTS = {
    "constant": -68.5,
    "soc": 22.2,
    "soc2": 449.1,
    "soc3": -568.3,
    "soc4": 138.6,
}


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


def _paper_soc(soc: float) -> float:
    if not math.isfinite(soc):
        raise ProfileError("battery correlation SOC must be finite")
    if not PENEKLIOGLU_SOC_RANGE[0] <= soc <= PENEKLIOGLU_SOC_RANGE[1]:
        raise ProfileError("paper correlation requires SOC in [0, 1]")
    return soc


def peneklioglu_2c_293p15_resistance_ohm(soc: float) -> float:
    """Figure 4.2의 2C·293.15 K 내부저항 ``Ri(SOC)`` [ohm]을 계산한다."""

    s = _paper_soc(soc)
    coefficients = PENEKLIOGLU_RESISTANCE_COEFFICIENTS
    # Horner form keeps the published polynomial explicit without extra powers.
    return (
        (
            coefficients["soc3"] * s
            + coefficients["soc2"]
        )
        * s
        + coefficients["soc"]
    ) * s + coefficients["constant"]


def peneklioglu_293p15_entropy_j_molk(soc: float) -> float:
    """Figure 4.6의 293.15 K 엔트로피 계수 ``ΔS(SOC)`` [J/(mol·K)]를 계산한다."""

    s = _paper_soc(soc)
    coefficients = PENEKLIOGLU_ENTROPY_COEFFICIENTS
    return (
        (
            (
                (
                    coefficients["soc4"] * s
                    + coefficients["soc3"]
                )
                * s
                + coefficients["soc2"]
            )
            * s
            + coefficients["soc"]
        )
        * s
        + coefficients["constant"]
    )


def peneklioglu_bernardi_2c_293p15_heat_terms_w(
    soc: float,
) -> tuple[float, float, float]:
    """PPT의 Bernardi식에서 비가역열, 가역열, 총 셀 발열 [W]을 반환한다.

    양의 4 A 방전전류를 사용하며 ``q_rev=-I*T*ΔS/F``의 부호를 그대로
    적용한다. 온도는 이 피팅식의 조건인 293.15 K로 고정되어 있다.
    """

    resistance_ohm = peneklioglu_2c_293p15_resistance_ohm(soc)
    entropy_j_molk = peneklioglu_293p15_entropy_j_molk(soc)
    current_a = PENEKLIOGLU_BERNARDI_CAPACITY_AH * PENEKLIOGLU_BERNARDI_C_RATE
    irreversible_w = current_a * current_a * resistance_ohm
    reversible_w = (
        -current_a
        * PENEKLIOGLU_BERNARDI_TEMPERATURE_K
        * entropy_j_molk
        / PENEKLIOGLU_FARADAY_C_MOL
    )
    return irreversible_w, reversible_w, irreversible_w + reversible_w


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
