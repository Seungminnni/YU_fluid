# Geometry overlay

`mdpi-cfd build-overlay` extracts `ppt/media/image4.png` from the original PPTX,
detects dark connected components, requires exactly 23 circular components in
the 5-4-5-4-5 topology, and calibrates pixels against the 114×121 mm enclosure
outline. The configured cell centres are then overlaid in magenta and compared
to the detected drawing centres in millimetres.

The generated files are:

- `top_overlay.svg`: original embedded image plus CAD ellipses and detected centres;
- `overlay_report.json`: threshold, calibration, measured pitch and per-cell residuals.

This closes the cell-layout part of Geometry Gate G1 reproducibly. It does not
close A-04: the 22-point outline and 19→11×11.25 mm notch profile are now
implemented, but the drawing does not directly dimension the port y coordinate
or inlet-column assignment. Therefore
`geometry_gate_g1_passed=false` remains mandatory until those dimensions are
approved.
