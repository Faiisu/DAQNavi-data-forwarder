# Replace legacy telemetry at the production schema cutover

The existing `daq_telemetry` table contains records whose physical or mockup origin cannot be established from the stored schema. At the coordinated production cutover, delete its legacy rows and old chunks, then create the production schema with explicit provenance and sample identity. The user accepted this irreversible removal so that the new production view begins with data whose origin and calibration are known.
