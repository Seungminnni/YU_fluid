"""Patch the OpenFOAM v2512 CHT solver to stop on the latest h residual.

OpenFOAM's loopControl checks the *first* solver residual retained within the
time step. For a transient CHT step this can keep running to the iteration cap
after both regions' most recent h solves have met the requested tolerance.
The build refuses to patch any other upstream source layout silently.
"""

from __future__ import annotations

import sys
from pathlib import Path


SOURCE = Path(sys.argv[1])
text = SOURCE.read_text(encoding="utf-8")
include_anchor = '#include "loopControl.H"\n'
if text.count(include_anchor) != 1:
    raise SystemExit("expected OpenFOAM v2512 loopControl include exactly once")
text = text.replace(include_anchor, include_anchor + '#include "solutionControl.H"\n')

anchor = '''                        fvMatrixAssemblyPtr->clear();
                    }
                }
            }
        }

        runTime.write();'''
replacement = '''                        fvMatrixAssemblyPtr->clear();
                    }

                    // The stock loopControl tests the first h residual in this
                    // time step. Test the most recent linear-solve initial
                    // residual in *every* fluid and solid region instead.
                    const scalar hTolerance =
                        pimple.subOrEmptyDict("energyCoupling")
                              .subOrEmptyDict("convergence")
                              .getOrDefault<scalar>("h", -1);
                    if (hTolerance > 0)
                    {
                        bool allRegionsSolved = true;
                        scalar maximumHResidual = 0;
                        forAll(fluidRegions, regioni)
                        {
                            const fvMesh& region = fluidRegions[regioni];
                            const dictionary& performance =
                                region.data().solverPerformanceDict();
                            bool foundH = false;
                            for (const entry& item : performance)
                            {
                                if (item.keyword() == "h")
                                {
                                    maximumHResidual = max
                                    (
                                        maximumHResidual,
                                        solutionControl::maxResidual
                                        (region, item).last()
                                    );
                                    foundH = true;
                                }
                            }
                            allRegionsSolved = allRegionsSolved && foundH;
                        }
                        forAll(solidRegions, regioni)
                        {
                            const fvMesh& region = solidRegions[regioni];
                            const dictionary& performance =
                                region.data().solverPerformanceDict();
                            bool foundH = false;
                            for (const entry& item : performance)
                            {
                                if (item.keyword() == "h")
                                {
                                    maximumHResidual = max
                                    (
                                        maximumHResidual,
                                        solutionControl::maxResidual
                                        (region, item).last()
                                    );
                                    foundH = true;
                                }
                            }
                            allRegionsSolved = allRegionsSolved && foundH;
                        }
                        reduce(allRegionsSolved, andOp<bool>());
                        reduce(maximumHResidual, maxOp<scalar>());
                        if (allRegionsSolved && maximumHResidual < hTolerance)
                        {
                            Info<< "adaptiveEnergyCoupling: latest h residual "
                                << maximumHResidual << " below " << hTolerance
                                << "; advancing time" << endl;
                            break;
                        }
                    }
                }
            }
        }

        runTime.write();'''
if text.count(anchor) != 1:
    raise SystemExit("expected OpenFOAM v2512 energy loop tail exactly once")
SOURCE.write_text(text.replace(anchor, replacement), encoding="utf-8")
