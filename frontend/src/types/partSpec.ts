// Strict mirror of backend/app/schemas/part_spec.py (PartSpec v2.0, ARCHITECTURE.md §6 + DRAWING_SPEC.md §5). Keep in sync.

export const REVIEW_CONFIDENCE_THRESHOLD = 0.85

export type OrthoFace = 'top' | 'bottom' | 'front' | 'back' | 'left' | 'right'
/** v2.0: also 'plane:<id>' of a tilted 3+2 working plane */
export type Face = OrthoFace | `plane:${string}`
export const FACES: OrthoFace[] = ['top', 'bottom', 'front', 'back', 'left', 'right']

export interface TiltedPlane {
  id: string
  name?: string | null
  origin_x: number
  origin_y: number
  origin_z: number
  rot_a_deg: number
  rot_b_deg: number
  rot_c_deg: number
  rotation_convention: 'SPATIAL_XYZ'
  confidence: number
  source_note?: string | null
}
export type DatumOrigin = 'bottom_left_top_face' | 'bottom_left_bottom_face' | 'center_top_face' | 'center_bottom_face'
export const DATUMS: DatumOrigin[] = ['bottom_left_top_face', 'bottom_left_bottom_face', 'center_top_face', 'center_bottom_face']
export type XY = [number, number]

export interface Stock {
  length_x: number
  width_y: number
  thickness_z: number
  confidence: number
}

export interface OuterProfile {
  type: 'rectangle' | 'rounded_rectangle' | 'polygon'
  corner_radius: number
  points_2d?: XY[] | null
  confidence: number
}

export type EdgeTarget = 'top_outer_edges' | 'bottom_outer_edges' | 'vertical_outer_edges' | 'all_outer_edges'
export const EDGE_TARGETS: EdgeTarget[] = ['top_outer_edges', 'bottom_outer_edges', 'vertical_outer_edges', 'all_outer_edges']

export interface EdgeTreatment {
  id: string
  type: 'chamfer' | 'fillet'
  size: number
  target: EdgeTarget
  confidence: number
}

export interface LinearPattern {
  type: 'linear'
  count_x: number
  count_y: number
  pitch_x: number
  pitch_y: number
}

export interface CircularPattern {
  type: 'circular'
  radius: number
  count: number
  start_angle_deg: number
  sweep_deg: number
}

export type Pattern = LinearPattern | CircularPattern

interface FeatureBase {
  id: string
  face: Face
  confidence: number
  source_note?: string | null
}

interface Positioned extends FeatureBase {
  center_x: number
  center_y: number
  pattern?: Pattern | null
  extra_positions?: XY[]
}

interface Cut {
  depth?: number | null
  through_all: boolean
  /** v2.0: meant to meet a cut from another face (no cross-face DFM error) */
  intentional_intersection?: boolean
}

export interface Hole extends Positioned, Cut {
  type: 'hole'
  diameter: number
  thread?: string | null
  thread_depth?: number | null
  counterbore?: { diameter: number; depth: number } | null
  countersink?: { diameter: number; angle_deg: number } | null
  tolerance?: string | null
  tip_angle_deg?: number | null
}

export interface PocketRect extends Positioned, Cut {
  type: 'pocket_rect'
  length_x: number
  width_y: number
  corner_radius: number
  rotation_deg: number
}

export interface PocketCircle extends Positioned, Cut {
  type: 'pocket_circle'
  diameter: number
}

export interface PocketPolygon extends FeatureBase, Cut {
  type: 'pocket_polygon'
  points_2d: XY[]
  corner_radius: number
}

export interface Slot extends FeatureBase, Cut {
  type: 'slot'
  start_x: number
  start_y: number
  end_x: number
  end_y: number
  width: number
}

export interface BossRect extends Positioned {
  type: 'boss_rect'
  length_x: number
  width_y: number
  height: number
  corner_radius: number
  rotation_deg: number
}

export interface BossCircle extends Positioned {
  type: 'boss_circle'
  diameter: number
  height: number
}

export interface StepCut extends FeatureBase {
  type: 'step_cut'
  x_min: number
  x_max: number
  y_min: number
  y_max: number
  depth: number
  corner_radius: number
}

/** v2.0: mills the inclined exterior face of a tilted plane (removes stock on its outer side) */
export interface AngledFaceCut extends FeatureBase {
  type: 'angled_face_cut'
  offset: number
}

export interface Complex3D extends FeatureBase {
  type: 'complex_3d'
  description: string
  operation: 'cut' | 'union'
  bbox_min?: [number, number, number] | null
  bbox_max?: [number, number, number] | null
  code?: string | null
}

export type Feature =
  | Hole
  | PocketRect
  | PocketCircle
  | PocketPolygon
  | Slot
  | BossRect
  | BossCircle
  | StepCut
  | AngledFaceCut
  | Complex3D

export type FeatureType = Feature['type']

export type InputSource = 'pdf_drawing' | 'paper_sketch' | 'dxf' | 'step' | 'manual'
export type SheetSize = 'AUTO' | 'A4_LANDSCAPE' | 'A3_LANDSCAPE'
export type DrawingLanguage = 'EN' | 'EN_NL'
export const ISO_SCALES = ['5:1', '2:1', '1:1', '1:2', '1:5', '1:10', '1:20', '1:50'] as const

export interface DrawingMetadata {
  sheet_size: SheetSize
  projection: 'ISO_FIRST_ANGLE'
  scale: string // 'AUTO' or 'a:b'
  language: DrawingLanguage
  surface_finish: string
  edge_break_note: string
  drawing_number?: string | null
  revision: string
  drawn_by: string
  checked_by: string
  approval_status: 'DRAFT' | 'APPROVED'
  /** hatched section A-A (EN ISO 128-50) when the part has hidden features */
  section_view?: 'AUTO' | 'NONE'
  /** EN ISO 1101 frames, printed as notes (not modelled in 3D) */
  gdt?: GeometricTolerance[]
  /** per-feature EN ISO 21920-1 requirements (the general one is surface_finish) */
  surface_finishes?: { feature_id: string; ra_um: number; note?: string }[]
  notes?: string[]
}

export type GdtCharacteristic =
  | 'straightness' | 'flatness' | 'circularity' | 'cylindricity' | 'profile_line' | 'profile_surface' | 'parallelism'
  | 'perpendicularity' | 'angularity' | 'position' | 'concentricity' | 'symmetry' | 'circular_runout' | 'total_runout'

export interface GeometricTolerance {
  characteristic: GdtCharacteristic
  tolerance_mm: number
  diameter_zone?: boolean
  datums?: string[]
  feature_id?: string | null
  note?: string
}

export interface MissingDimension {
  field_path: string
  prompt_nl: string
  prompt_en?: string
  suggested_default?: number | null
  blocking: boolean
}

export interface PartSpec {
  schema_version: '1.0' | '1.1' | '2.0'
  input_source?: InputSource | null
  part_number?: string | null
  revision?: string | null
  drawing_metadata?: DrawingMetadata
  missing_dimensions?: MissingDimension[]
  part_name?: string | null
  units: 'mm'
  material?: string | null
  general_tolerance?: string | null
  datum_origin: DatumOrigin
  stock: Stock
  outer_profile: OuterProfile
  tilted_planes?: TiltedPlane[]
  features: Feature[]
  edge_treatments: EdgeTreatment[]
  requires_complex_3d_fallback: boolean
  ambiguities: string[]
}

// ---- DFM report (§8) ------------------------------------------------------------

export type Severity = 'info' | 'warning' | 'error'

export interface DFMIssue {
  code: string
  severity: Severity
  message: string
  feature_id?: string | null
  suggestion?: string | null
}

export interface GeometryCheck {
  is_valid: boolean
  solid_count: number
  volume_mm3: number
  bbox_min_mm: [number, number, number]
  bbox_max_mm: [number, number, number]
  expected_size_mm: [number, number, number]
  bbox_matches_spec: boolean
  bbox_deviation_mm: number
}

export interface FeatureBuildStatus {
  feature_id: string
  ok: boolean
  method: 'deterministic' | 'llm_sandbox' | 'skipped'
  error?: string | null
}

export interface DFMReport {
  spec_version: number
  passed: boolean
  geometry?: GeometryCheck | null
  issues: DFMIssue[]
  feature_status: FeatureBuildStatus[]
  review_items: string[]
  colored_faces: Record<string, number>
  stats: Record<string, number>
}

// ---- Jobs (§7) --------------------------------------------------------------------

export type JobStatus =
  | 'queued'
  | 'parsing_pdf'
  | 'extracting_features'
  | 'awaiting_dimensions'
  | 'compiling_cad'
  | 'verifying'
  | 'retrying'
  | 'ready'
  | 'failed'
export const TERMINAL: JobStatus[] = ['ready', 'failed', 'awaiting_dimensions']

export interface JobSummary {
  job_id: string
  status: JobStatus
  filename: string
  created_at: number
  updated_at: number
  progress: number
  message?: string | null
  current_version: number
  error?: string | null
  running: boolean
  dfm: { passed: boolean; errors: number; warnings: number; needs_review: boolean; ambiguities: number } | null
  verified_at?: number | null
  batch_id?: string | null
  part_number?: string | null
  revision?: string | null
}

export interface SpecVersion {
  version: number
  source: 'extraction' | 'reextract' | 'self_heal' | 'critic' | 'complex_code' | 'user_edit' | 'dxf' | 'step' | 'sketch'
  reason: string
  created_at: number
}

export interface CriticReport {
  verdict: 'match' | 'mismatch' | 'skipped'
  discrepancies: { feature_id?: string | null; issue: string; suggested_fix?: string | null }[]
}

export interface IterationLog {
  ts: number
  iteration: number | 'operator'
  version: number
  gate_passed: boolean
  errors: string[]
  action?: string | null
  critic?: CriticReport | null
  duration_s?: number
}

export interface JobDetail extends JobSummary {
  version: number
  versions: SpecVersion[]
  part_spec: PartSpec | null
  dfm_report: DFMReport | null
  iteration_logs: IterationLog[]
  critic: CriticReport | null
  artifacts: Record<string, string> & { tiles?: Record<string, string> }
  drawing?: DrawingInfo | null
  /** STEP intake: is the recognised PartSpec a complete description (else edits keep the uploaded geometry)? */
  step_import?: { complete: boolean; offset_mm: number[]; volume_step_mm3: number; volume_spec_mm3: number | null } | null
  duration_s?: number
}

/** versions/v<n>/drawing.json (EN ISO detail drawing of that revision). */
export interface DrawingInfo {
  version: number
  sheet_size: Exclude<SheetSize, 'AUTO'>
  scale: string
  warnings: string[]
  balloons: string[]
  overrides: { sheet_size?: SheetSize; scale?: string; language?: DrawingLanguage; section_view?: 'AUTO' | 'NONE'; engine?: string }
  duration_s?: number
}

export interface PipelineEvent {
  id: number
  job_id: string
  ts: number
  stage: string
  level: 'info' | 'warning' | 'error'
  message: string
  progress: number | null
  data: Record<string, unknown> | null
}

export function needsReview(confidence: number): boolean {
  return confidence < REVIEW_CONFIDENCE_THRESHOLD
}
