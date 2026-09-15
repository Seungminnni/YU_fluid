/*
 * peneklioglu_2C_293K_heat_source.c
 *
 * ANSYS Fluent UDF: transient volumetric heat generation for an 18650 Li-ion cell.
 *
 * Model basis:
 *   Peneklioglu, Ph.D. thesis (2024), Eq. (3.1):
 *
 *       q = I^2 * Ri - I * T * DeltaS / F      [W per cell]
 *
 *   For 2C, 293.15 K:
 *       Ri(SOC) = 0.0994 - 0.145*SOC + 0.246*SOC^2 - 0.126*SOC^3  [ohm]
 *
 *   For 293.15 K:
 *       DeltaS(SOC) =
 *           -68.5
 *           + 22.2*SOC
 *           + 449.1*SOC^2
 *           - 568.3*SOC^3
 *           + 138.6*SOC^4                                  [J/(mol K)]
 *
 * Target geometry / operating condition from Aydin et al.:
 *   capacity = 2 Ah
 *   C-rate   = 2C -> I = 4 A
 *   diameter = 19 mm
 *   length   = 68 mm
 *   discharge duration at 2C = 1800 s
 *
 * IMPORTANT:
 *   This is NOT the unpublished UDF from Aydin et al.
 *   It is a literature-based substitute heat-generation model using
 *   Peneklioglu's 18650 correlations, applied to the Aydin geometry/2C condition.
 *
 *   The Ri/DeltaS correlations are fitted at 293.15 K. Therefore this default
 *   implementation uses T_REF = 293.15 K in the thermodynamic term. If you
 *   later implement temperature interpolation using the 298.15/303.15 K
 *   correlations, replace the fixed-temperature functions accordingly.
 */

#include "udf.h"
#include <math.h>

/* ---------- Physical / operating constants ---------- */

#define FARADAY_CONST        96485.33212   /* C/mol */
#define CELL_CAPACITY_AH     2.0           /* Ah */
#define C_RATE               2.0           /* 2C */
#define DISCHARGE_CURRENT_A  (CELL_CAPACITY_AH * C_RATE)  /* 4 A */

#define CELL_DIAMETER_M      0.019         /* m */
#define CELL_LENGTH_M        0.068         /* m */
#define PI_CONST             3.14159265358979323846
#define CELL_VOLUME_M3       (PI_CONST * 0.25 * CELL_DIAMETER_M * CELL_DIAMETER_M * CELL_LENGTH_M)

#define T_REF_K              293.15        /* K: correlation temperature */
#define DISCHARGE_TIME_S     (3600.0 / C_RATE) /* 1800 s from 100% to 0% SOC */

/*
 * Set to 0 (recommended for the 293.15 K fitted correlation):
 *     use T_REF_K in reversible heat term.
 *
 * Set to 1 only for sensitivity testing:
 *     use local Fluent cell temperature C_T(c,t).
 *     NOTE: Ri and DeltaS are still the 293.15 K fitted curves, so this is
 *     not a full Ri(SOC,T), DeltaS(SOC,T) model.
 */
#define USE_LOCAL_CELL_T     0


/* ---------- Helper functions ---------- */

static real clamp_real(real x, real lo, real hi)
{
    if (x < lo) return lo;
    if (x > hi) return hi;
    return x;
}


/* SOC = 1 at t=0, SOC = 0 at t=1800 s for a 2C constant-current discharge. */
static real soc_from_time(real time_s)
{
    real soc = 1.0 - time_s / DISCHARGE_TIME_S;
    return clamp_real(soc, 0.0, 1.0);
}


/* Peneklioglu Fig. 4.2: Group 7, 2C, 293.15 K. Unit: ohm. */
static real internal_resistance_2C_293K(real soc)
{
    real s2 = soc * soc;
    real s3 = s2 * soc;

    return 0.0994
         - 0.145  * soc
         + 0.246  * s2
         - 0.126  * s3;
}


/* Peneklioglu Fig. 4.6: 293.15 K. Unit: J/(mol K). */
static real entropy_coefficient_293K(real soc)
{
    real s2 = soc * soc;
    real s3 = s2 * soc;
    real s4 = s3 * soc;

    return -68.5
         +  22.2 * soc
         + 449.1 * s2
         - 568.3 * s3
         + 138.6 * s4;
}


/*
 * Cell total heat generation [W]:
 *
 *     q = I^2 Ri - I T DeltaS / F
 *
 * First term  : irreversible heat
 * Second term : reversible/entropic heat
 */
static real cell_heat_generation_W(real soc, real temperature_K)
{
    const real I = DISCHARGE_CURRENT_A;
    real Ri = internal_resistance_2C_293K(soc);
    real DeltaS = entropy_coefficient_293K(soc);

    real q_irreversible = I * I * Ri;
    real q_reversible   = -I * temperature_K * DeltaS / FARADAY_CONST;

    return q_irreversible + q_reversible;
}


/* -------------------------------------------------------------------------
 * Main transient UDF
 *
 * Hook this function to:
 *   Cell Zone Conditions -> battery solid zone -> Source Terms -> Energy
 *
 * Return value is volumetric heat source [W/m^3].
 * Apply the same UDF to each of the 23 battery solid zones.
 * ------------------------------------------------------------------------- */
DEFINE_SOURCE(peneklioglu_dynamic_heat_source, c, t, dS, eqn)
{
    real time_s = CURRENT_TIME;
    real soc;
    real temperature_K;
    real q_cell_W;
    real q_vol_W_m3;

    /*
     * After the nominal 2C discharge has ended, switch the discharge source off.
     * A simulation intended only for 0~1800 s is unaffected by this branch.
     */
    if (time_s > DISCHARGE_TIME_S)
    {
        dS[eqn] = 0.0;
        return 0.0;
    }

    soc = soc_from_time(time_s);

#if USE_LOCAL_CELL_T
    temperature_K = C_T(c, t);
#else
    temperature_K = T_REF_K;
#endif

    q_cell_W = cell_heat_generation_W(soc, temperature_K);
    q_vol_W_m3 = q_cell_W / CELL_VOLUME_M3;

#if USE_LOCAL_CELL_T
    /*
     * d(source)/dT for Fluent linearization.
     * Ri and DeltaS are treated as fixed with respect to local T here
     * because the selected fits are the 293.15 K curves.
     */
    dS[eqn] =
        (-DISCHARGE_CURRENT_A * entropy_coefficient_293K(soc) / FARADAY_CONST)
        / CELL_VOLUME_M3;
#else
    dS[eqn] = 0.0;
#endif

    return q_vol_W_m3;
}


/*
 * This delivery intentionally exposes only the dynamic source above.
 * The 16,325 W/m^3 constant baseline is not compiled into this UDF, preventing
 * it from being selected accidentally in place of the transient model.
 */
