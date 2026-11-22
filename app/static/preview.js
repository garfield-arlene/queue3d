// Pre-submission 3D preview: renders a user-selected STL file entirely in
// the browser (parsed straight from the File object, no server round-trip)
// on a grid sized to the printer's actual build plate, with mouse/touch
// orbit + zoom. This only shows what the raw model looks like - it does
// NOT reflect slicing (supports, orientation the slicer might choose,
// etc.); see README.md's To do list for where that's headed next.
//
// Bed dimensions are the one supported printer's (see
// slicing/profiles/makerbot-plus-tough-extruder.json): 295x195mm bed,
// 165mm build height.

import * as THREE from "three";
import { STLLoader } from "./vendor/three/addons/loaders/STLLoader.js";
import { OrbitControls } from "./vendor/three/addons/controls/OrbitControls.js";
import { TransformControls } from "./vendor/three/addons/controls/TransformControls.js";

const BED_WIDTH_MM = 295;
const BED_DEPTH_MM = 195;
const BED_HEIGHT_MM = 165;

// The camera-framing floor in renderGeometry() below, matching the
// bed's own largest dimension rather than an arbitrary small constant -
// see that function's own comment for why.
const BED_RADIUS_MM = Math.max(BED_WIDTH_MM, BED_DEPTH_MM, BED_HEIGHT_MM);
// Scale bounds - the same as jobs.py's MIN_SCALE_FACTOR/MAX_SCALE_FACTOR
// and job_edit.html's scale field, so the drag handles, auto-fit, the
// field and the server all agree. 100x (10000%) because a model exported
// in the wrong units can be ~1 mm across and need that much.
const MIN_SCALE = 0.01;
const MAX_SCALE = 100.0;
// jobs.MAX_OFFSET_MM - how far from the middle of the plate a model can be moved.
const MAX_OFFSET_MM = 295;
// How far inside the plate's edges "Auto-resize to fit build plate" stops.
const FIT_MARGIN_MM = 0.5;

const loader = new STLLoader();
let scene, camera, renderer, controls, container, infoEl;
let currentMesh = null;
// Where currentMesh sits: the middle of the placed model's outline, on the
// plate. Its geometry is stored relative to that point, so the on-canvas
// handles appear on the model itself rather than at the middle of the
// plate - see renderGeometry.
const meshHome = new THREE.Vector3();
// How far above the plate the slicer put the model's underside - the
// raft's height when it's printed on one (models.Job.raft_enabled), 0
// otherwise. Comes with the supports (see showSupports).
let modelLift = 0;
let currentSupports = null;
let resizeObserver = null;
// The model exactly as loaded/parsed, before any scaling - kept around so
// setPreviewScale()/autoFitScale() below always compute from the true
// original size, not whatever scale happened to be applied last. Cloned
// fresh into renderGeometry() on every (re)scale rather than mutated in
// place, so repeated scale changes never compound.
let rawGeometry = null;
// Size in mm of what's currently displayed (after rotation and scale), or
// null before anything has loaded - see displayedSize().
let displayedSizeMm = null;

// On-canvas rotate/resize handles (Three.js's own TransformControls addon -
// the "handles" a user drags directly, as opposed to typing into the
// rotate/scale number fields or clicking a face in "snap to surface" mode).
// Created once in initScene() and reused across model (re)loads, same as
// `controls` (OrbitControls) above - only its `object`/`mode`/`visible`
// change per use. `gizmoBaseline` is the transform the gizmo's *own*
// object3D delta composes on top of (see commitGizmoTransform below); kept
// here rather than re-derived from the DOM, since preview.js has no access
// to job_edit.html's input fields - the caller hands it in via
// enableGizmo() and reads the result back out via the onCommit callback.
let transformControls = null;
let gizmoBaseline = { rotateX: 0, rotateY: 0, rotateZ: 0, scale: 1, offsetX: 0, offsetY: 0 };
let gizmoOnCommit = null;

function initScene(containerEl) {
    container = containerEl;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x222222);

    camera = new THREE.PerspectiveCamera(45, 1, 1, 5000);
    // Looking down at the bed at an angle, roughly one bed-diagonal away -
    // a reasonable default framing before we know the model's size.
    camera.position.set(BED_WIDTH_MM * 0.7, -BED_DEPTH_MM * 1.1, BED_HEIGHT_MM * 1.1);
    camera.up.set(0, 0, 1); // Z-up, matching the STL/slicer convention

    renderer = new THREE.WebGLRenderer({ antialias: true });
    container.innerHTML = "";
    container.appendChild(renderer.domElement);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(0, 0, BED_HEIGHT_MM * 0.15);
    controls.enableDamping = true;

    // Starts detached/invisible - only turned on by enableGizmo() below,
    // when the caller (job_edit.html) puts a rotate/resize gizmo button
    // into "active" state. dragging-changed is the documented way to stop
    // OrbitControls from also trying to orbit the camera while a handle is
    // being dragged - both listen on the same canvas, and without this an
    // attempted drag on a handle would instead (or also) spin the camera.
    transformControls = new TransformControls(camera, renderer.domElement);
    transformControls.enabled = false;
    transformControls.visible = false;
    scene.add(transformControls);
    transformControls.addEventListener("dragging-changed", (e) => {
        controls.enabled = !e.value;
    });
    transformControls.addEventListener("objectChange", () => {
        // "Resize... while maintaining the aspect ratio" is a hard product
        // requirement (see applyTransform's own comment) - forcing this on
        // every change event, regardless of which individual scale handle
        // was actually grabbed, means every drag in scale mode behaves as
        // a single uniform resize rather than needing three handles that
        // each only sort-of do the right thing.
        if (transformControls.mode === "scale" && transformControls.object) {
            const s = transformControls.object.scale;
            s.y = s.x;
            s.z = s.x;
        }
    });
    transformControls.addEventListener("mouseUp", () => commitGizmoTransform());

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(1, -1, 2);
    scene.add(dirLight);

    // Build plate: a grid plus a faint solid plane so orientation is
    // unambiguous even from directly above.
    const grid = new THREE.GridHelper(Math.max(BED_WIDTH_MM, BED_DEPTH_MM), 10, 0x888888, 0x444444);
    grid.rotation.x = Math.PI / 2; // GridHelper is XZ-plane by default; rotate to lie flat on XY (Z-up)
    scene.add(grid);

    const bedGeometry = new THREE.PlaneGeometry(BED_WIDTH_MM, BED_DEPTH_MM);
    const bedMaterial = new THREE.MeshBasicMaterial({
        color: 0x3a6ea5,
        transparent: true,
        opacity: 0.15,
        side: THREE.DoubleSide,
    });
    scene.add(new THREE.Mesh(bedGeometry, bedMaterial));

    buildSizeReference();

    resizeObserver = new ResizeObserver(() => resizeToContainer());
    resizeObserver.observe(container);
    // Called both synchronously now *and* again next frame, deliberately -
    // a real bug, not belt-and-suspenders for its own sake. Reading
    // container.clientWidth/clientHeight right here can race the browser's
    // own layout pass (this runs immediately after container.innerHTML=""
    // and appending a brand new canvas above), occasionally landing before
    // the container has actually taken on its real CSS-computed size -
    // sizing the renderer/camera from whatever transitional value that
    // read caught, with nothing ever correcting it afterward unless
    // something else happens to trigger the ResizeObserver later (e.g. the
    // user resizing their browser window - which is exactly how this was
    // first noticed: a live page showing a totally blank preview until an
    // unrelated window resize "fixed" it). requestAnimationFrame runs
    // after the browser's next layout/paint, so this second call is
    // guaranteed to see the real, settled size even when the immediate one
    // didn't - cheap enough to always do, not just when something looks
    // wrong, since there's no reliable way to detect "that first read was
    // actually the wrong one" from in here.
    resizeToContainer();
    requestAnimationFrame(() => resizeToContainer());

    animate();
}

/*
 * Size reference: a US quarter and a dollar bill, to scale, lying on the
 * plate in front of the model - things everyone knows the size of, for
 * judging how big the print really is. They're in the scene, so they zoom
 * with the model, and coloured silver and green, unlike the model (blue,
 * red when it doesn't fit) or the snap highlight (amber); they're never
 * part of what's sliced. A button in the corner of the preview shows and
 * hides them, remembered per browser.
 */
const QUARTER_DIAMETER_MM = 24.26;
const QUARTER_THICKNESS_MM = 1.75;
const BILL_WIDTH_MM = 155.96;
const BILL_HEIGHT_MM = 66.29;
const BILL_THICKNESS_MM = 0.2; // really 0.11 - a little thicker to stay visible edge-on
const SIZE_REFERENCE_GAP_MM = 8;
const SIZE_REFERENCE_KEY = "queue3d.sizeReference";
let sizeReference = null;
let quarterMesh = null;
let billMesh = null;

function labelTexture(width, height, draw) {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    draw(canvas.getContext("2d"), width, height);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = 4;
    return texture;
}

function sizeReferenceShown() {
    try {
        return localStorage.getItem(SIZE_REFERENCE_KEY) === "1";
    } catch {
        return false;
    }
}

function buildSizeReference() {
    sizeReference = new THREE.Group();

    const silver = new THREE.MeshPhongMaterial({ color: 0xb8bcc4, specular: 0x666666, shininess: 60 });
    const coinFace = new THREE.MeshPhongMaterial({
        specular: 0x666666,
        shininess: 60,
        map: labelTexture(256, 256, (ctx, w, h) => {
            ctx.fillStyle = "#b8bcc4";
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = "#7d828c";
            ctx.lineWidth = 10;
            ctx.beginPath();
            ctx.arc(w / 2, h / 2, w / 2 - 16, 0, Math.PI * 2);
            ctx.stroke();
            ctx.fillStyle = "#3d424a";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.font = "bold 84px sans-serif";
            ctx.fillText("25¢", w / 2, h / 2 - 16);
            ctx.font = "34px sans-serif";
            ctx.fillText("24 mm", w / 2, h / 2 + 56);
        }),
    });
    // A cylinder's caps face up and down its axis (Y); turned to stand on
    // Z, then a quarter turn so the label reads the same way as the bill's.
    const coin = new THREE.CylinderGeometry(QUARTER_DIAMETER_MM / 2, QUARTER_DIAMETER_MM / 2, QUARTER_THICKNESS_MM, 64);
    coin.rotateX(Math.PI / 2);
    coin.rotateZ(Math.PI / 2);
    quarterMesh = new THREE.Mesh(coin, [silver, coinFace, silver]);
    sizeReference.add(quarterMesh);

    const green = new THREE.MeshPhongMaterial({ color: 0x6f9a6a, shininess: 10 });
    const billFace = new THREE.MeshPhongMaterial({
        shininess: 10,
        map: labelTexture(1024, 436, (ctx, w, h) => {
            ctx.fillStyle = "#cfe0c3";
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = "#3e6b3a";
            ctx.lineWidth = 18;
            ctx.strokeRect(24, 24, w - 48, h - 48);
            ctx.fillStyle = "#3e6b3a";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.font = "bold 120px sans-serif";
            ctx.fillText("$1 bill", w / 2, h / 2 - 40);
            ctx.font = "56px sans-serif";
            ctx.fillText("156 x 66 mm", w / 2, h / 2 + 80);
        }),
    });
    // BoxGeometry's faces: +x, -x, +y, -y, +z (top), -z.
    billMesh = new THREE.Mesh(
        new THREE.BoxGeometry(BILL_WIDTH_MM, BILL_HEIGHT_MM, BILL_THICKNESS_MM),
        [green, green, green, green, billFace, green]
    );
    sizeReference.add(billMesh);

    sizeReference.visible = sizeReferenceShown();
    scene.add(sizeReference);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "size-reference-btn";
    button.title = "A US quarter and dollar bill, to scale, to judge the model's size by. They aren't printed.";
    const label = () => { button.textContent = sizeReference.visible ? "Hide coin & bill" : "Show coin & bill"; };
    label();
    // The preview's own pointer handlers (orbit, Snap to surface) mustn't
    // see a click on the button as a click on the model behind it.
    for (const type of ["pointerdown", "pointerup", "pointermove"]) {
        button.addEventListener(type, (e) => e.stopPropagation());
    }
    button.addEventListener("click", () => {
        sizeReference.visible = !sizeReference.visible;
        try {
            localStorage.setItem(SIZE_REFERENCE_KEY, sizeReference.visible ? "1" : "0");
        } catch {
            // Not remembered, still toggled.
        }
        label();
    });
    container.appendChild(button);
}

/** Lays the bill and quarter side by side just in front of the model. */
function placeSizeReference(placedBox) {
    if (!sizeReference) return;
    const y = placedBox.min.y - SIZE_REFERENCE_GAP_MM - BILL_HEIGHT_MM / 2;
    const billX = placedBox.min.x + BILL_WIDTH_MM / 2;
    billMesh.position.set(billX, y, BILL_THICKNESS_MM / 2);
    quarterMesh.position.set(
        billX + BILL_WIDTH_MM / 2 + SIZE_REFERENCE_GAP_MM + QUARTER_DIAMETER_MM / 2,
        y,
        QUARTER_THICKNESS_MM / 2
    );
}

function resizeToContainer() {
    const width = container.clientWidth || 400;
    const height = container.clientHeight || 400;
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
}

function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
}

/**
 * Area-weighted centroid of `geometry`'s triangles, projected to X/Y - the
 * exact same calculation as slicing/stl_to_3mf.py's surface_centroid_xy(),
 * which must stay in lockstep with this one (see showModel()'s own
 * centering comment for why: this app slices already-centered geometry,
 * and the preview has to show that same placement, not an independently
 * -arrived-at one that happens to differ for an asymmetric model).
 * Tessellation-independent, unlike a plain vertex average - a triangle's
 * own area weights it, not how many vertices happen to be nearby.
 */
function surfaceCentroidXY(geometry) {
    const pos = geometry.attributes.position;
    const index = geometry.index;
    const triCount = (index ? index.count : pos.count) / 3;
    const vx = (i) => pos.getX(index ? index.getX(i) : i);
    const vy = (i) => pos.getY(index ? index.getX(i) : i);

    let totalArea = 0;
    let weightedX = 0;
    let weightedY = 0;
    for (let t = 0; t < triCount; t++) {
        const i0 = t * 3, i1 = t * 3 + 1, i2 = t * 3 + 2;
        const ax = vx(i0), ay = vy(i0);
        const bx = vx(i1), by = vy(i1);
        const cx = vx(i2), cy = vy(i2);
        const area = Math.abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) / 2;
        weightedX += ((ax + bx + cx) / 3) * area;
        weightedY += ((ay + by + cy) / 3) * area;
        totalArea += area;
    }
    if (totalArea === 0) return { x: 0, y: 0 };
    return { x: weightedX / totalArea, y: weightedY / totalArea };
}

/**
 * True if every edge of `geometry`'s mesh is shared by exactly two
 * triangles, traversed in opposite directions - the precondition
 * volumeCentroidXY() below needs to be meaningful at all. Exact JS
 * counterpart of slicing/stl_to_3mf.py's _is_watertight_consistent(),
 * which must stay in lockstep with this one for the same reason
 * surfaceCentroidXY() already does (see that function's own comment).
 *
 * Keyed by each edge endpoint's own coordinates (rounded to a fixed
 * precision), not by buffer index, unlike the Python side - STLLoader
 * here never calls setIndex() (confirmed directly against its source),
 * so every triangle in this app's actual geometry owns independent
 * vertex copies even along a shared edge, the same way the raw STL
 * format itself does. Two coincident vertices from different triangles
 * are only recognizable as "the same point" by comparing their actual
 * coordinates, which is exactly what Python's own parse_stl() does too
 * (its vertex-dedup dict is keyed by the exact (x,y,z) tuple) - this is
 * the equivalent check, just performed inline per-edge instead of via a
 * separate up-front dedup pass.
 */
function isWatertightConsistent(geometry) {
    const pos = geometry.attributes.position;
    const index = geometry.index;
    const triCount = (index ? index.count : pos.count) / 3;
    const vi = (i) => (index ? index.getX(i) : i);
    const key = (i) => `${pos.getX(vi(i)).toFixed(5)},${pos.getY(vi(i)).toFixed(5)},${pos.getZ(vi(i)).toFixed(5)}`;

    const directedEdges = new Map();
    for (let t = 0; t < triCount; t++) {
        const i0 = t * 3, i1 = t * 3 + 1, i2 = t * 3 + 2;
        const k0 = key(i0), k1 = key(i1), k2 = key(i2);
        for (const [u, v] of [[k0, k1], [k1, k2], [k2, k0]]) {
            const edgeKey = `${u}|${v}`;
            directedEdges.set(edgeKey, (directedEdges.get(edgeKey) || 0) + 1);
        }
    }
    for (const [edgeKey, count] of directedEdges) {
        if (count !== 1) return false;
        const [u, v] = edgeKey.split("|");
        if ((directedEdges.get(`${v}|${u}`) || 0) !== 1) return false;
    }
    return true;
}

/**
 * True 3D center-of-mass of `geometry`'s enclosed volume, projected to
 * X/Y, or null if the precondition (isWatertightConsistent above)
 * isn't met or the computed total volume comes out ~zero (a degenerate/
 * flat mesh, or one broken enough that the formula's own signed
 * contributions cancelled out to noise). Exact JS counterpart of
 * slicing/stl_to_3mf.py's volume_centroid_xy() - see that function's
 * own docstring for why this exists (surface_centroid_xy's gap for a
 * strongly asymmetric model) and the divergence-theorem formula this
 * computes. Works directly on the raw, non-indexed triangle positions -
 * unlike the watertightness check above, this sum doesn't need shared
 * vertex identity at all, only each triangle's own three positions.
 */
function volumeCentroidXY(geometry) {
    if (!isWatertightConsistent(geometry)) return null;
    const pos = geometry.attributes.position;
    const index = geometry.index;
    const triCount = (index ? index.count : pos.count) / 3;
    const vx = (i) => pos.getX(index ? index.getX(i) : i);
    const vy = (i) => pos.getY(index ? index.getX(i) : i);
    const vz = (i) => pos.getZ(index ? index.getX(i) : i);

    let totalVolume = 0;
    let weightedX = 0;
    let weightedY = 0;
    for (let t = 0; t < triCount; t++) {
        const i0 = t * 3, i1 = t * 3 + 1, i2 = t * 3 + 2;
        const ax = vx(i0), ay = vy(i0), az = vz(i0);
        const bx = vx(i1), by = vy(i1), bz = vz(i1);
        const cx = vx(i2), cy = vy(i2), cz = vz(i2);
        const signedVol = (
            ax * (by * cz - bz * cy) +
            ay * (bz * cx - bx * cz) +
            az * (bx * cy - by * cx)
        ) / 6;
        weightedX += signedVol * (ax + bx + cx) / 4;
        weightedY += signedVol * (ay + by + cy) / 4;
        totalVolume += signedVol;
    }
    if (Math.abs(totalVolume) < 1e-9) return null;
    return { x: weightedX / totalVolume, y: weightedY / totalVolume };
}

/**
 * The actual centroid to center a model on - the true volume centroid
 * when it's computable (volumeCentroidXY above), falling back to the
 * area-weighted surface projection (surfaceCentroidXY) otherwise. Every
 * caller that needs "where is this model's center" uses this, not
 * surfaceCentroidXY directly, so renderGeometry()'s own centering and
 * autoFitScale()'s centroid-relative distance check can never disagree
 * with each other - exact JS counterpart of slicing/stl_to_3mf.py's
 * center_vertices() using the same volume-first, surface-fallback
 * logic, which this must stay in lockstep with for the same reason
 * surfaceCentroidXY's own comment already explains.
 */
function centroidXY(geometry) {
    return volumeCentroidXY(geometry) || surfaceCentroidXY(geometry);
}

/**
 * Renders `geometry` (already scaled by the caller, if at all) - the
 * shared body behind both showModel() (a fresh load) and
 * setPreviewScale() (re-rendering the same raw model at a new scale,
 * with no re-fetch/re-parse needed). `offset` ({x, y}, mm) moves it on
 * the plate after centering - models.Job.offset_x/offset_y, applied
 * the same way by slicing/stl_to_3mf.build_3mf.
 */
function renderGeometry(geometry, offset = { x: 0, y: 0 }) {
    clearSnapTarget(); // it's drawn on the mesh being replaced
    if (currentMesh) {
        scene.remove(currentMesh);
        currentMesh.geometry.dispose();
        currentMesh.material.dispose();
        currentMesh = null;
    }

    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const size = new THREE.Vector3();
    box.getSize(size);

    // Some STL exports are malformed (NaN/garbage coordinates from a buggy
    // CAD tool, or an empty/degenerate mesh) - computeBoundingBox() then
    // yields non-finite values. Report that clearly rather than rendering
    // a mesh with invalid geometry (which can silently fail to draw at all,
    // or worse, still show a fragment of garbage) or "NaN x NaN x NaN mm".
    if (![size.x, size.y, size.z].every(Number.isFinite)) {
        if (infoEl) {
            infoEl.textContent = "Couldn't determine this model's size - the file may have invalid or empty geometry.";
            infoEl.classList.add("error");
        }
        return;
    }

    displayedSizeMm = { x: size.x, y: size.y, z: size.z };
    geometry.computeVertexNormals();

    // Center the model on the bed in X/Y and drop it so its lowest point
    // sits on the plate (Z=0) - the same "auto place on bed" a slicer does.
    // The true volume centroid when computable, falling back to the
    // area-weighted surface projection otherwise (see centroidXY's own
    // comment) - not the bounding-box midpoint either way:
    // slicing/stl_to_3mf.py's matching center_vertices() uses the
    // identical volume-first, surface-fallback logic, since an
    // asymmetric model (most of its surface or volume well off from its
    // own box's middle) can pass bbox-centering fine here but then fail
    // the slicer's own downstream bed-centering check, which looks at
    // where the sliced material actually ends up, not the box. Confirmed
    // against a real model that failed exactly that way before this
    // changed.
    const centroid = centroidXY(geometry);
    geometry.translate(-centroid.x + (offset.x || 0), -centroid.y + (offset.y || 0), -box.min.z);

    // Whether it actually fits has to be checked against this final,
    // already-centered placement - not the raw pre-translate size
    // compared symmetrically against the bed. Centroid centering can be
    // wildly asymmetric for a lopsided model (a real fighter-jet model
    // hit this directly: one side sits ~127mm from the centroid, the
    // other only ~68mm) - the *total* span can still be under the bed's
    // own dimension while one side alone hangs off the edge. A naive
    // "does the total span fit" check (what this used to do) can't catch
    // that; recomputing the bounding box post-translate and checking
    // each side against the bed's actual half-extent can.
    geometry.computeBoundingBox();
    const placedBox = geometry.boundingBox;
    const halfWidth = BED_WIDTH_MM / 2;
    const halfDepth = BED_DEPTH_MM / 2;
    const overBuildVolume =
        placedBox.min.x < -halfWidth || placedBox.max.x > halfWidth ||
        placedBox.min.y < -halfDepth || placedBox.max.y > halfDepth ||
        placedBox.max.z > BED_HEIGHT_MM;

    const material = new THREE.MeshPhongMaterial({
        color: overBuildVolume ? 0xcc4433 : 0x3a9ad9,
        specular: 0x111111,
        shininess: 40,
    });

    // Stored relative to the middle of its outline and positioned there,
    // so the move/rotate/resize handles sit on the model. Rotation and
    // scale stay baked into the vertices; only this translation isn't.
    meshHome.set((placedBox.min.x + placedBox.max.x) / 2, (placedBox.min.y + placedBox.max.y) / 2, modelLift);
    geometry.translate(-meshHome.x, -meshHome.y, 0);
    currentMesh = new THREE.Mesh(geometry, material);
    currentMesh.position.copy(meshHome);
    scene.add(currentMesh);
    placeSizeReference(placedBox);

    // Frame the camera around the model AND the bed together, holding a
    // floor tied to the bed's own fixed size (BED_RADIUS_MM) rather than
    // a small, model-size-only minimum. The real bug this fixes: every
    // model used to get framed to fill the view similarly regardless of
    // its actual size relative to the bed - a genuinely tiny model and a
    // genuinely oversized one both just "filled the frame" and looked
    // similarly normal-sized, with only the separate text warning (see
    // infoEl below) saying otherwise. A model actually bigger than the
    // bed now visibly overflows this bed-anchored view instead of simply
    // re-filling the frame to match it; a tiny one now visibly reads as
    // small against a consistently bed-scaled view instead of filling
    // the frame on its own. near/far scale with it too: a fixed far
    // plane would clip (render nothing for) a model large enough to need
    // a camera distance beyond it - a real risk here specifically, since
    // "too large for the build plate" is often a units mistake (e.g.
    // meters exported as if mm) rather than a merely-somewhat-oversized
    // model, and can be 1000x larger than expected.
    const modelSpan = Math.max(size.x, size.y, size.z, 1);
    const radius = Math.max(modelSpan, BED_RADIUS_MM);
    // near scales with the model's own span, not the bed-anchored radius
    // below - otherwise a small model's minDistance (also model-span-
    // based, a few lines down) could end up closer than near itself,
    // clipping the model out of view the moment someone actually zoomed
    // in close on it. far still uses the bed-anchored radius - that
    // direction has no equivalent clipping risk, only wasted depth range.
    camera.near = Math.max(modelSpan / 1000, 0.01);
    camera.far = radius * 20;
    camera.updateProjectionMatrix();
    camera.position.set(radius * 1.4, -radius * 1.6, radius * 1.3);
    controls.target.set(0, 0, size.z / 2);
    // OrbitControls' own zoom (mouse wheel / pinch) has no limit by
    // default (minDistance 0, maxDistance Infinity) - nothing stopped it
    // scrolling the camera distance past near or far above, at which
    // point the model just clips out of view entirely: turning still
    // worked (orbit doesn't change distance), but zooming either
    // direction far enough made the model disappear completely. Minimum
    // distance is tied to the model's own span, not the bed-anchored
    // radius above - a small model should still be zoomable in close for
    // inspection, not stuck behind the bed-scale framing floor; maximum
    // distance uses the bed-anchored radius, since zooming out further
    // than that is harmless either way. Both comfortably inside near/far.
    controls.minDistance = modelSpan * 0.1;
    controls.maxDistance = radius * 15;
    controls.update();

    if (infoEl) {
        const dims = `${size.x.toFixed(1)} x ${size.y.toFixed(1)} x ${size.z.toFixed(1)} mm`;
        // "doesn't fit," not "too large" - an asymmetric model can be
        // small enough overall but still hang off one edge once centered
        // on its actual surface centroid (see the comment above).
        // Small enough for the plate but placed so part of it hangs off -
        // moving it fixes that, which is a much smaller change than
        // shrinking or rotating it.
        const onlyPlacement = overBuildVolume &&
            size.x <= BED_WIDTH_MM && size.y <= BED_DEPTH_MM && size.z <= BED_HEIGHT_MM;
        infoEl.textContent = !overBuildVolume
            ? dims
            : onlyPlacement
                ? `${dims} - hangs off the edge of the build plate. Click "Center on plate" to move it back on.`
                : `${dims} - doesn't fit the build plate (${BED_WIDTH_MM} x ${BED_DEPTH_MM} x ${BED_HEIGHT_MM} mm)`;
        infoEl.classList.toggle("error", overBuildVolume);
    }
}

function showModel(geometry) {
    rawGeometry = geometry.clone();
    modelLift = 0;
    renderGeometry(geometry);
}

/**
 * Applies `rotateX`/`rotateY`/`rotateZ` (degrees, about the fixed world
 * axes, in that exact order) and then `scale` (uniform) to the raw
 * model, then re-renders - no re-fetch/re-parse, and always computed
 * fresh from the true original geometry (see rawGeometry above), so
 * repeated calls never compound. A no-op if nothing's loaded yet.
 *
 * Matches slicing/stl_to_3mf.build_3mf's own rotate-then-scale-then-
 * center ordering exactly (see that function's docstring), so what this
 * shows is what will actually get sliced. The rotation order (X, then
 * Y, then Z, each about the *world* axis, not the model's own
 * progressively-tilted local axes) is the one BufferGeometry.rotateX/Y/Z
 * already implement natively - deliberately not Three.js's Euler/
 * Quaternion "XYZ" order, which is a *different*, intrinsic-axis
 * convention that does NOT reproduce this (confirmed by direct
 * comparison while building this - see computeSnapRotation below for
 * where that distinction actually matters). Scale is always uniform, so
 * proportions can never distort, per the user ("resize... while
 * maintaining the aspect ratio").
 */
export function applyTransform({ scale = 1, rotateX = 0, rotateY = 0, rotateZ = 0, offsetX = 0, offsetY = 0 } = {}) {
    if (!rawGeometry) return;
    renderGeometry(transformedGeometry({ scale, rotateX, rotateY, rotateZ }), { x: offsetX, y: offsetY });
}

/** A rotated and scaled copy of the raw model, not yet placed. */
function transformedGeometry({ scale = 1, rotateX = 0, rotateY = 0, rotateZ = 0 } = {}) {
    const geometry = rawGeometry.clone();
    if (rotateX) geometry.rotateX(THREE.MathUtils.degToRad(rotateX));
    if (rotateY) geometry.rotateY(THREE.MathUtils.degToRad(rotateY));
    if (rotateZ) geometry.rotateZ(THREE.MathUtils.degToRad(rotateZ));
    if (scale !== 1) geometry.scale(scale, scale, scale);
    return geometry;
}

/**
 * "Center on plate": the {offsetX, offsetY} (mm) that puts the middle of
 * the model's outline at the middle of the plate, for this rotation and
 * scale. The default placement centers its centroid instead (see
 * renderGeometry), which for a lopsided model - a wrench with a solid
 * head and a thin handle - leaves one end hanging off the plate even
 * when the whole model is shorter than the plate. {0, 0} if nothing's
 * loaded.
 */
export function centerOnPlateOffset(transform = {}) {
    if (!rawGeometry) return { offsetX: 0, offsetY: 0 };
    const geometry = transformedGeometry(transform);
    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const centroid = centroidXY(geometry);
    const offsetX = centroid.x - (box.min.x + box.max.x) / 2;
    const offsetY = centroid.y - (box.min.y + box.max.y) / 2;
    if (![offsetX, offsetY].every(Number.isFinite)) return { offsetX: 0, offsetY: 0 };
    return { offsetX, offsetY };
}

/**
 * Finishes one gizmo drag (called on TransformControls' own "mouseUp"
 * event, i.e. the handle was released - not fired for a mere hover/click
 * that never actually dragged anything). `transformControls.object` is
 * whatever Mesh enableGizmo() last attached to; because that mesh's own
 * Object3D rotation and scale always start at identity (see renderGeometry - every
 * (re)render creates a fresh Mesh with default position/quaternion/scale,
 * all real transform baked directly into vertex data instead), its
 * quaternion/scale *after* a drag session is exactly that session's delta,
 * not an absolute value - the same "delta composes onto a running
 * baseline" shape as computeSnapRotation above, and reusing the exact same
 * confirmed 'ZYX' intrinsic-Euler equivalence for the rotation half of it.
 *
 * Re-bakes the result via applyTransform() (replacing currentMesh with a
 * fresh one, same as every other transform change in
 * this file) and re-attaches the gizmo to that new mesh - without this,
 * the gizmo would keep pointing at a mesh renderGeometry has already
 * disposed, and the next drag would silently do nothing.
 */
function commitGizmoTransform() {
    if (!transformControls || !transformControls.object) return;
    const obj = transformControls.object;

    const eulerToQuat = (xDeg, yDeg, zDeg) =>
        new THREE.Quaternion().setFromEuler(
            new THREE.Euler(
                THREE.MathUtils.degToRad(xDeg),
                THREE.MathUtils.degToRad(yDeg),
                THREE.MathUtils.degToRad(zDeg),
                "ZYX"
            )
        );
    const baseQuat = eulerToQuat(gizmoBaseline.rotateX, gizmoBaseline.rotateY, gizmoBaseline.rotateZ);
    // This drag's delta, composed on top of the baseline it started from -
    // same order as computeSnapRotation's own snapQuat.multiply(currentQuat).
    const combined = obj.quaternion.clone().multiply(baseQuat);
    const resultEuler = new THREE.Euler().setFromQuaternion(combined, "ZYX");

    // obj.scale.x is this drag's *relative* factor (forced uniform by the
    // objectChange listener above) on top of whatever scale the model was
    // already displayed at - not an absolute value either.
    let scale = gizmoBaseline.scale * obj.scale.x;
    // TransformControls' own free-scale ("XYZ" handle) math computes its
    // factor as pointEnd.length()/pointStart.length() in an invisible
    // helper plane - if a drag happens to start very close to the
    // object's own origin in that plane (pointStart.length() near zero),
    // the result can come out wildly large, or even negative if the drag
    // crossed the origin (a real, known characteristic of the underlying
    // library's free-scale handle, confirmed by hitting it directly while
    // testing this - not something specific to this app's own code).
    // Clamped to the exact bounds jobs.py's start_reslice() enforces
    // server-side (MIN_SCALE_FACTOR/MAX_SCALE_FACTOR) - a negative or
    // astronomical scale is never something "resize while keeping
    // proportions" should show, let alone let through to a re-slice.
    if (!Number.isFinite(scale) || scale <= 0) scale = gizmoBaseline.scale;
    scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));

    // A move drag shows up as the mesh's own position - its distance
    // from meshHome is this drag's delta, added to where the model
    // already was. Clamped to
    // jobs.MAX_OFFSET_MM, the same bound the server enforces.
    const clampOffset = (v) => Math.min(MAX_OFFSET_MM, Math.max(-MAX_OFFSET_MM, Number.isFinite(v) ? v : 0));
    const newTransform = {
        rotateX: THREE.MathUtils.radToDeg(resultEuler.x),
        rotateY: THREE.MathUtils.radToDeg(resultEuler.y),
        rotateZ: THREE.MathUtils.radToDeg(resultEuler.z),
        scale,
        offsetX: clampOffset(gizmoBaseline.offsetX + obj.position.x - meshHome.x),
        offsetY: clampOffset(gizmoBaseline.offsetY + obj.position.y - meshHome.y),
    };
    gizmoBaseline = newTransform;
    applyTransform(newTransform);
    transformControls.attach(currentMesh);

    if (gizmoOnCommit) gizmoOnCommit(newTransform);
}

/**
 * Turns on the on-canvas rotate/resize/move handles in `mode` ('rotate',
 * 'scale' or 'translate'), starting from `baseline` (the transform the model is
 * currently displayed at - typically the caller's own number-field
 * values) and calling `onCommit(newTransform)` once per completed drag
 * with the new absolute {rotateX, rotateY, rotateZ, scale, offsetX, offsetY}. The caller
 * (job_edit.html) is expected to write those straight back into its own
 * number fields - the gizmo and the fields are two views onto the same
 * state, kept in sync through this callback rather than the gizmo owning
 * its own separate notion of the model's orientation/size.
 *
 * Safe to call again while already enabled (e.g. after the caller's own
 * number fields changed and replaced currentMesh via applyTransform) -
 * re-attaches to whatever the current mesh is and refreshes the baseline
 * a fresh drag should compose onto. A no-op if nothing's loaded yet.
 */
export function enableGizmo(mode, baseline, onCommit) {
    if (!transformControls || !currentMesh) return;
    gizmoBaseline = {
        rotateX: baseline.rotateX || 0,
        rotateY: baseline.rotateY || 0,
        rotateZ: baseline.rotateZ || 0,
        scale: baseline.scale || 1,
        offsetX: baseline.offsetX || 0,
        offsetY: baseline.offsetY || 0,
    };
    gizmoOnCommit = onCommit || null;
    transformControls.attach(currentMesh);
    transformControls.mode = mode;
    // Moving is across the plate only - lifting it off or pushing it
    // through the plate isn't a placement the slicer can use.
    transformControls.showZ = mode !== "translate";
    transformControls.enabled = true;
    transformControls.visible = true;
}

/** Turns the on-canvas handles off entirely (plain orbit/zoom resumes). */
export function disableGizmo() {
    if (!transformControls) return;
    transformControls.detach();
    transformControls.enabled = false;
    transformControls.visible = false;
    gizmoOnCommit = null;
}

/**
 * The largest scale factor that fits the model on the build plate on all
 * three axes *at the given rotation* - rotating changes the footprint, so
 * a model already reoriented (by hand, or via "snap to surface") needs
 * auto-fit computed against that orientation's actual geometry, not the
 * as-uploaded one. Returns 1 if nothing's loaded.
 *
 * Assumes the model is centered on the plate by its outline (see
 * centerOnPlateOffset) - job_edit.html's "Auto-resize to fit build plate"
 * centers it along with the resize. Fitting against the default centroid
 * placement instead left a lopsided model (a fighter jet: ~127mm of it on
 * one side of its centroid, ~68mm on the other) much smaller than the
 * plate allowed.
 */
/** The displayed model's size in mm ({x, y, z}, after the current
 * rotation and scale), or null if nothing's loaded. */
export function displayedSize() {
    return displayedSizeMm;
}

export function autoFitScale(rotateX = 0, rotateY = 0, rotateZ = 0) {
    if (!rawGeometry) return 1;
    const geometry = rawGeometry.clone();
    if (rotateX) geometry.rotateX(THREE.MathUtils.degToRad(rotateX));
    if (rotateY) geometry.rotateY(THREE.MathUtils.degToRad(rotateY));
    if (rotateZ) geometry.rotateZ(THREE.MathUtils.degToRad(rotateZ));
    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const size = new THREE.Vector3();
    box.getSize(size);
    if (![size.x, size.y, size.z].every(Number.isFinite)) return 1;

    // Fitted for the model centered on the plate by its outline (see
    // centerOnPlateOffset) - job_edit.html centers it along with the
    // resize - so half its size each way, however lopsided it is.
    // FIT_MARGIN_MM short of each edge, so rounding the size and position
    // the form keeps (2 decimal places) can't tip it just over one.
    const halfExtentX = size.x / 2;
    const halfExtentY = size.y / 2;

    // Enlarges as well as shrinks: the largest scale that fits. This used
    // to be capped at 1 (100%), so on a model smaller than the plate the
    // button reset the scale to 100% - a real 1.2 mm model that needed
    // enlarging just snapped back to its original size.
    return Math.min(
        MAX_SCALE,
        (BED_WIDTH_MM / 2 - FIT_MARGIN_MM) / halfExtentX,
        (BED_DEPTH_MM / 2 - FIT_MARGIN_MM) / halfExtentY,
        (BED_HEIGHT_MM - FIT_MARGIN_MM) / size.z
    );
}

/** Convex hull of 2D points (flat [x0, y0, x1, y1, ...]), as [[x, y], ...]. */
function convexHull2D(flat) {
    const points = [];
    for (let i = 0; i < flat.length; i += 2) points.push([flat[i], flat[i + 1]]);
    points.sort((p, q) => p[0] - q[0] || p[1] - q[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [];
    for (const p of points) {
        while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
        lower.push(p);
    }
    const upper = [];
    for (let i = points.length - 1; i >= 0; i--) {
        const p = points[i];
        while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
        upper.push(p);
    }
    return lower.slice(0, -1).concat(upper.slice(0, -1));
}

/**
 * A turn about the vertical (degrees, to add to rotateZ) that makes the
 * model's footprint fit the plate when centered by its outline (see
 * centerOnPlateOffset): 0 if it already fits, otherwise the one leaving
 * the most room to the nearest edge, or null if no turn makes it fit (it
 * needs shrinking - see autoFitScale). Snap to surface
 * uses it: a face picked for how the model should lie says nothing about
 * which way round it should point, and a long model - a 266 mm wrench -
 * laid down across the 195 mm depth of the plate hangs off when turning
 * it would fit it.
 *
 * Adding to rotateZ is the same as turning the placed model, since Z is
 * the last rotation applied, about the fixed vertical axis.
 */
export function fitTurnOnPlate(transform = {}) {
    if (!rawGeometry) return null;
    const position = transformedGeometry(transform).attributes.position;
    const flat = new Float64Array(position.count * 2);
    for (let i = 0; i < position.count; i++) {
        flat[i * 2] = position.getX(i);
        flat[i * 2 + 1] = position.getY(i);
    }
    const hull = convexHull2D(flat);
    const width = BED_WIDTH_MM - 2 * FIT_MARGIN_MM;
    const depth = BED_DEPTH_MM - 2 * FIT_MARGIN_MM;
    // mm to spare at the tightest edge after this turn; negative if it
    // hangs off.
    const room = (degrees) => {
        const r = THREE.MathUtils.degToRad(degrees);
        const cos = Math.cos(r), sin = Math.sin(r);
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (const [x, y] of hull) {
            const tx = x * cos - y * sin, ty = x * sin + y * cos;
            minX = Math.min(minX, tx); maxX = Math.max(maxX, tx);
            minY = Math.min(minY, ty); maxY = Math.max(maxY, ty);
        }
        return Math.min(width - (maxX - minX), depth - (maxY - minY));
    };
    if (room(0) >= 0) return 0;
    // The turn that only just squeezes a long model in on the diagonal
    // isn't the one to pick when lying it along the plate leaves plenty
    // of room. A footprint turned half way round is the same size, so 90
    // degrees either way covers every possibility; to the nearest mm of
    // room, the smaller turn wins.
    let best = null;
    let bestRoom = 0;
    for (let step = 1; step <= 90; step++) {
        for (const turn of step < 90 ? [step, -step] : [step]) {
            const exact = room(turn);
            const r = Math.round(exact);
            if (exact >= 0 && (best === null || r > bestRoom)) { best = turn; bestRoom = r; }
        }
    }
    return best;
}

/**
 * "Snap to surface": given the currently-applied rotation (degrees,
 * same X/Y/Z-in-that-order convention as applyTransform above) and
 * where the user clicked on the preview canvas (`clientX`/`clientY`,
 * straight from a MouseEvent), raycasts against the displayed model; if
 * a face was actually hit, returns the new {x, y, z} rotation (degrees)
 * that would make that face the new bottom, flat on the plate. Returns
 * null if nothing was hit (a click that missed the model) or nothing's
 * loaded yet - the caller should leave the current rotation alone in
 * that case, not treat it as "reset to zero."
 *
 * The math this needs - composing an *additional* rotation on top of
 * whatever's already applied, then expressing the combined result back
 * as three sequential X/Y/Z angles - cannot use Three.js's Euler/
 * Quaternion conversions with the "XYZ" order string some documentation
 * suggests: that order is intrinsic (each axis is the model's own,
 * already-tilted-by-the-previous-rotation axis), while
 * BufferGeometry.rotateX/Y/Z composes extrinsically (each axis is the
 * fixed world axis, unaffected by earlier rotations) - confirmed these
 * two conventions actually disagree by direct comparison before writing
 * this, not assumed from documentation. The extrinsic X-then-Y-then-Z
 * composition this app uses everywhere else is equivalent to Three.js's
 * *intrinsic* "ZYX" order instead (a standard, general equivalence
 * between intrinsic and extrinsic Euler angles in reverse order) -
 * confirmed correct via a real round-trip test (compose two rotations
 * as quaternions using Euler "ZYX", decompose back to angles the same
 * way, and verify applying those angles sequentially reproduces the
 * directly-composed quaternion's result) before relying on it here.
 */
export function computeSnapRotation(currentRotateX, currentRotateY, currentRotateZ, clientX, clientY) {
    if (!rawGeometry || !currentMesh || !renderer || !camera) return null;

    // The face that will sit on the plate - a whole flat face, not the one
    // triangle under the pointer (see pickRestFace). Its normal is in
    // currentMesh's own space, where rotation and scale are baked into the
    // vertices and the only transform is a translation, which doesn't
    // change a normal.
    const picked = pickRestFace(clientX, clientY);
    if (!picked) return null;
    const clickedNormal = picked.normal.clone();

    const eulerToQuat = (xDeg, yDeg, zDeg) =>
        new THREE.Quaternion().setFromEuler(
            new THREE.Euler(
                THREE.MathUtils.degToRad(xDeg),
                THREE.MathUtils.degToRad(yDeg),
                THREE.MathUtils.degToRad(zDeg),
                "ZYX"
            )
        );

    const currentQuat = eulerToQuat(currentRotateX, currentRotateY, currentRotateZ);
    // The rotation that takes the clicked face's normal to "straight
    // down" (0,0,-1) - the definition of "this face is now the bottom,
    // resting flat on the plate."
    const snapQuat = new THREE.Quaternion().setFromUnitVectors(clickedNormal, new THREE.Vector3(0, 0, -1));
    // Apply the existing rotation first, then the snap correction on
    // top of that (not the other way around) - the snap is computed
    // from the *currently displayed* orientation, so it has to compose
    // after it, not before.
    const combined = snapQuat.clone().multiply(currentQuat);
    const result = new THREE.Euler().setFromQuaternion(combined, "ZYX");
    return {
        x: THREE.MathUtils.radToDeg(result.x),
        y: THREE.MathUtils.radToDeg(result.y),
        z: THREE.MathUtils.radToDeg(result.z),
    };
}

/*
 * Snap to surface lays the model on a whole flat face, not on the one
 * triangle under the pointer. A model file is made of small triangles,
 * and much of what looks like a flat side is really bevels, rounded rims
 * and the walls of holes - on a real Voronoi-handle wrench only about half
 * of its side, seen face on, was actually flat, so a click had close to
 * even odds of tilting the model to whatever slope it landed on.
 *
 * "Rest faces" are the flat faces the model can actually lie on: a plane
 * with a meaningful amount of the model's surface in it (real contact with
 * the plate, not just a few edges) and nothing poking out past it. Found
 * once per loaded model, in rawGeometry's space; their triangles are
 * looked up in the displayed mesh, whose vertex order is the same, so they
 * follow any rotation, scale or move.
 */
let restFaces = null;
let restFacesFor = null;
let displayFaces = null;
let displayFacesFor = null;
let hoverMesh = null;
let hoverKey = null;
// A plane holding less than this share of the model's surface isn't worth
// resting on (a sliver of contact, or a curve's tiny flat facets).
const REST_FACE_MIN_SHARE = 0.005;
// Only the largest candidate planes are checked for "nothing past it".
const REST_FACE_MAX_CANDIDATES = 64;

function triangleCorners(position, index, tri, a, b, c) {
    const i = tri * 3;
    a.fromBufferAttribute(position, index ? index.getX(i) : i);
    b.fromBufferAttribute(position, index ? index.getX(i + 1) : i + 1);
    c.fromBufferAttribute(position, index ? index.getX(i + 2) : i + 2);
}

function findRestFaces() {
    if (restFacesFor === rawGeometry) return restFaces;
    const position = rawGeometry.attributes.position;
    const index = rawGeometry.index;
    const triCount = (index ? index.count : position.count) / 3;
    rawGeometry.computeBoundingBox();
    const diag = rawGeometry.boundingBox.getSize(new THREE.Vector3()).length();
    // How far off a plane still counts as on it - relative, since a model
    // in the wrong units can be 1000x off its real size.
    const tol = Math.max(diag * 0.0005, 1e-6);

    // Group triangles by plane: normal to about half a degree, distance
    // from the origin to a few tolerances.
    const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
    const ab = new THREE.Vector3(), ac = new THREE.Vector3();
    const buckets = new Map();
    const triNormals = new Float32Array(triCount * 3);
    let totalArea = 0;
    for (let t = 0; t < triCount; t++) {
        triangleCorners(position, index, t, a, b, c);
        ab.subVectors(b, a);
        ac.subVectors(c, a);
        const n = ab.cross(ac);
        const len = n.length();
        if (!(len > 0)) continue;
        const area = len / 2;
        totalArea += area;
        n.divideScalar(len);
        n.toArray(triNormals, t * 3);
        const d = n.dot(a);
        const key = `${Math.round(n.x * 100)},${Math.round(n.y * 100)},${Math.round(n.z * 100)},${Math.round(d / (tol * 4))}`;
        let bucket = buckets.get(key);
        if (!bucket) {
            bucket = { normal: new THREE.Vector3(), d: 0, area: 0, tris: [] };
            buckets.set(key, bucket);
        }
        bucket.normal.addScaledVector(n, area);
        bucket.d += d * area;
        bucket.area += area;
        bucket.tris.push(t);
    }

    // Rejoin planes the rounding split across neighbouring groups.
    const minArea = totalArea * REST_FACE_MIN_SHARE;
    const groups = [...buckets.values()]
        .filter((g) => g.area >= minArea / 4)
        .sort((g, h) => h.area - g.area)
        .slice(0, 400);
    const cosOneDegree = Math.cos(THREE.MathUtils.degToRad(1));
    const planes = [];
    for (const g of groups) {
        const n = g.normal.clone().normalize();
        const d = g.d / g.area;
        const same = planes.find((p) => p.normal.dot(n) > cosOneDegree && Math.abs(p.d - d) < tol * 4);
        if (same) {
            same.tris.push(...g.tris);
            same.area += g.area;
        } else {
            planes.push({ normal: n, d, area: g.area, tris: g.tris.slice() });
        }
    }
    const candidates = planes
        .filter((p) => p.area >= minArea)
        .sort((p, q) => q.area - p.area)
        .slice(0, REST_FACE_MAX_CANDIDATES);

    // Keep only planes with nothing beyond them - the model can't lie on a
    // face that something else sticks out past. One pass over every vertex.
    const highest = candidates.map(() => -Infinity);
    const lowest = candidates.map(() => Infinity);
    const v = new THREE.Vector3();
    for (let i = 0; i < position.count; i++) {
        v.fromBufferAttribute(position, i);
        for (let k = 0; k < candidates.length; k++) {
            const s = candidates[k].normal.dot(v);
            if (s > highest[k]) highest[k] = s;
            if (s < lowest[k]) lowest[k] = s;
        }
    }
    restFaces = [];
    candidates.forEach((p, k) => {
        // Normals follow the triangles' winding, which some exports get
        // backwards - an outermost plane facing in is still outermost.
        let flip;
        if (highest[k] - p.d <= tol * 2) flip = false;
        else if (p.d - lowest[k] <= tol * 2) flip = true;
        else return;
        restFaces.push({ normal: p.normal, tris: p.tris, side: [], flip, key: `face${restFaces.length}` });
    });
    // The rest of that side of the model: everything parallel to a rest
    // face but set back from it - a wrench's thinner handle next to its
    // thicker head. Pointing at it still means "lay it on this side"; it
    // just won't touch the plate.
    for (let t = 0; t < triCount; t++) {
        const nx = triNormals[t * 3], ny = triNormals[t * 3 + 1], nz = triNormals[t * 3 + 2];
        for (const face of restFaces) {
            if (face.normal.x * nx + face.normal.y * ny + face.normal.z * nz > cosOneDegree) {
                face.side.push(t);
                break;
            }
        }
    }
    restFacesFor = rawGeometry;
    return restFaces;
}

/**
 * The rest faces in the displayed mesh's own space: outward normal, plane
 * offset, and the face's extent within its plane, recomputed whenever the
 * displayed mesh changes.
 */
function displayRestFaces() {
    if (displayFacesFor === currentMesh) return displayFaces;
    const geometry = currentMesh.geometry;
    const position = geometry.attributes.position;
    const index = geometry.index;
    const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
    const ab = new THREE.Vector3(), ac = new THREE.Vector3();
    displayFaces = findRestFaces().map((face) => {
        // Area-weighted normal over the whole face, so one sliver triangle
        // can't skew it.
        const normal = new THREE.Vector3();
        for (const t of face.tris) {
            triangleCorners(position, index, t, a, b, c);
            normal.add(ab.subVectors(b, a).cross(ac.subVectors(c, a)));
        }
        normal.normalize();
        if (face.flip) normal.negate();
        const u = new THREE.Vector3(1, 0, 0);
        if (Math.abs(normal.x) > 0.9) u.set(0, 1, 0);
        u.sub(normal.clone().multiplyScalar(u.dot(normal))).normalize();
        const w = new THREE.Vector3().crossVectors(normal, u);
        let d = -Infinity, minU = Infinity, maxU = -Infinity, minW = Infinity, maxW = -Infinity;
        for (const t of face.tris) {
            triangleCorners(position, index, t, a, b, c);
            for (const p of [a, b, c]) d = Math.max(d, normal.dot(p));
        }
        // Its extent covers the whole side (see findRestFaces), holes,
        // bevels and all.
        for (const t of face.side) {
            triangleCorners(position, index, t, a, b, c);
            for (const p of [a, b, c]) {
                const pu = u.dot(p), pw = w.dot(p);
                minU = Math.min(minU, pu); maxU = Math.max(maxU, pu);
                minW = Math.min(minW, pw); maxW = Math.max(maxW, pw);
            }
        }
        return { key: face.key, tris: face.tris, side: face.side, normal, d, u, w, minU, maxU, minW, maxW };
    });
    displayFacesFor = currentMesh;
    return displayFaces;
}

/**
 * What a Snap to surface click at (clientX, clientY) would lay the model
 * on: {normal, tris, side, key}, in currentMesh's own space, or null when the
 * pointer isn't over the model.
 *
 * Every rest face is on the outside of the model, so together they wrap it
 * like a box. The pointer's line of sight enters that box through the
 * face it's looking at - the last of the planes facing it to be crossed -
 * whether it then lands on flat material, a bevel, or the wall of a hole.
 * If it enters off the edge of any rest face (a rounded top, say, with no
 * flat face to lie on), this falls back to the triangle actually under
 * the pointer.
 */
function pickRestFace(clientX, clientY) {
    if (!rawGeometry || !currentMesh || !renderer || !camera) return null;
    const rect = renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(
        ((clientX - rect.left) / rect.width) * 2 - 1,
        -((clientY - rect.top) / rect.height) * 2 + 1
    );
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(ndc, camera);
    const hits = raycaster.intersectObject(currentMesh, false);
    if (hits.length === 0 || !hits[0].face) return null;

    const origin = raycaster.ray.origin.clone().sub(currentMesh.position);
    const dir = raycaster.ray.direction;
    let best = null;
    let bestT = -Infinity;
    for (const face of displayRestFaces()) {
        const facing = face.normal.dot(dir);
        if (facing >= -1e-6) continue; // seen from behind or edge-on
        const t = (face.d - face.normal.dot(origin)) / facing;
        if (t > bestT) { bestT = t; best = face; }
    }
    if (best) {
        const entry = origin.clone().addScaledVector(dir, bestT);
        const pu = best.u.dot(entry), pw = best.w.dot(entry);
        const slack = 0.02 * Math.max(best.maxU - best.minU, best.maxW - best.minW);
        if (pu >= best.minU - slack && pu <= best.maxU + slack && pw >= best.minW - slack && pw <= best.maxW + slack) {
            return { normal: best.normal.clone(), tris: best.tris, side: best.side, key: best.key };
        }
    }
    const tri = hits[0].faceIndex;
    return { normal: hits[0].face.normal.clone().normalize(), tris: [tri], side: [tri], key: `tri${tri}` };
}

/**
 * Snap to surface's hover highlight: tints the side a click at (clientX,
 * clientY) would lay the model on - bright where it will touch the plate,
 * dimmer where it's parallel but set back - or clears it when the pointer
 * is off the model.
 */
export function showSnapTarget(clientX, clientY) {
    const picked = pickRestFace(clientX, clientY);
    const key = picked ? picked.key : null;
    if (key === hoverKey && (!hoverMesh || hoverMesh.parent === currentMesh)) return;
    clearSnapTarget();
    if (!picked) return;
    const source = currentMesh.geometry;
    const index = source.index;
    const touching = new Set(picked.tris);
    const setBack = picked.side.filter((t) => !touching.has(t));
    const ordered = [...picked.tris, ...setBack];
    const corners = new Float32Array(ordered.length * 9);
    const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
    ordered.forEach((t, i) => {
        triangleCorners(source.attributes.position, index, t, a, b, c);
        a.toArray(corners, i * 9);
        b.toArray(corners, i * 9 + 3);
        c.toArray(corners, i * 9 + 6);
    });
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(corners, 3));
    geometry.addGroup(0, picked.tris.length * 3, 0);
    geometry.addGroup(picked.tris.length * 3, setBack.length * 3, 1);
    // Drawn over the model's own surface, which it sits exactly on.
    const tint = (color) => new THREE.MeshBasicMaterial({
        color,
        side: THREE.DoubleSide,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2,
    });
    hoverMesh = new THREE.Mesh(geometry, [tint(0xffb000), tint(0xc08a20)]);
    currentMesh.add(hoverMesh);
    hoverKey = key;
}

export function clearSnapTarget() {
    if (hoverMesh) {
        hoverMesh.removeFromParent();
        hoverMesh.geometry.dispose();
        for (const material of hoverMesh.material) material.dispose();
        hoverMesh = null;
    }
    hoverKey = null;
}

const SUPPORT_TUBE_RADIUS = 0.3; // mm - roughly a support strand's width

function showSupports(data) {
    if (currentSupports) {
        scene.remove(currentSupports);
        currentSupports.geometry.dispose();
        currentSupports.material.dispose();
        currentSupports = null;
    }
    // {segments, model_lift}, or (written before 9.6.0) just the segments.
    const segments = Array.isArray(data) ? data : (data && data.segments) || [];
    modelLift = (!Array.isArray(data) && data && Number(data.model_lift)) || 0;
    if (currentMesh) {
        // Up onto the raft, so it sits where the slicer put it.
        meshHome.z = modelLift;
        currentMesh.position.z = modelLift;
    }
    if (segments.length === 0) return;

    // Support segments come from the slicer's own gcode coordinates (see
    // app/supports.py), which are relative to the bed origin the same way
    // our own bed grid is - not re-centered the way showModel() re-centers
    // an arbitrary uploaded STL to its own bounding box. They should land
    // in the same place the model does as long as the slicer placed the
    // model at the bed center, which is its default for a single object.
    //
    // Rendered as small solid tubes (one instanced cylinder per segment),
    // not thin lines - a real print's supports are a solid material, and
    // 1px lines read as a sparse wireframe/scatter of dots rather than
    // something solid, especially with only a subset of layers shown (see
    // app/supports.py's TARGET_LAYERS - full toolpath density is out of
    // scope, so making the most of the layers we do show matters more).
    // One shared low-poly cylinder, transformed per instance, so this
    // stays cheap even at 100k+ segments for a support-dense model.
    // 4-sided (not rounder) deliberately - at this radius and density a
    // support-dense model can mean hundreds of thousands of instances, and
    // the cross-section is thin enough that the extra sides of a rounder
    // cylinder wouldn't be visible anyway. Worth reducing further if a
    // real client device on the deployment network (see project memory
    // queue3d-deployment-network) struggles with the resulting triangle
    // count; this dev machine isn't necessarily representative of that.
    const unitCylinder = new THREE.CylinderGeometry(1, 1, 1, 4, 1, true);
    const material = new THREE.MeshPhongMaterial({ color: 0xffa500 }); // orange - distinct from the model
    currentSupports = new THREE.InstancedMesh(unitCylinder, material, segments.length);

    const up = new THREE.Vector3(0, 1, 0);
    const start = new THREE.Vector3();
    const end = new THREE.Vector3();
    const mid = new THREE.Vector3();
    const dir = new THREE.Vector3();
    const quaternion = new THREE.Quaternion();
    const scale = new THREE.Vector3();
    const matrix = new THREE.Matrix4();

    segments.forEach((seg, i) => {
        start.set(seg[0], seg[1], seg[2]);
        end.set(seg[3], seg[4], seg[5]);
        const length = start.distanceTo(end);
        if (length < 0.001) {
            // Degenerate (zero-length) segment - place a tiny dot rather
            // than fail the quaternion calc on an undefined direction.
            scale.set(SUPPORT_TUBE_RADIUS, 0.01, SUPPORT_TUBE_RADIUS);
            matrix.compose(start, quaternion.identity(), scale);
            currentSupports.setMatrixAt(i, matrix);
            return;
        }
        mid.copy(start).add(end).multiplyScalar(0.5);
        dir.copy(end).sub(start).normalize();
        quaternion.setFromUnitVectors(up, dir);
        scale.set(SUPPORT_TUBE_RADIUS, length, SUPPORT_TUBE_RADIUS);
        matrix.compose(mid, quaternion, scale);
        currentSupports.setMatrixAt(i, matrix);
    });
    currentSupports.instanceMatrix.needsUpdate = true;
    scene.add(currentSupports);
}

/**
 * Set up (or reuse) a preview in `containerEl`, showing `file` (a File/Blob
 * - typically from an <input type="file">). `infoElement`, if given, gets
 * the model's dimensions (or an over-size warning) as text.
 */
export function previewFile(file, containerEl, infoElement) {
    infoEl = infoElement || null;
    if (!scene) {
        initScene(containerEl);
    }
    if (infoEl) infoEl.textContent = "Loading...";

    const reader = new FileReader();
    reader.onload = (e) => {
        try {
            const geometry = loader.parse(e.target.result);
            showModel(geometry);
        } catch (err) {
            if (infoEl) {
                infoEl.textContent = "Couldn't read this file as an STL.";
                infoEl.classList.add("error");
            }
        }
    };
    reader.onerror = () => {
        if (infoEl) {
            infoEl.textContent = "Couldn't read this file.";
            infoEl.classList.add("error");
        }
    };
    reader.readAsArrayBuffer(file);
}

/**
 * Set up (or reuse) a preview in `containerEl`, loading a model (and
 * optionally its generated supports, shown as solid orange tubes) from
 * server URLs rather than a local File - used for viewing an already
 * sliced job. `supportsUrl` is optional; pass null/undefined to skip it.
 */
export async function previewUrl(stlUrl, supportsUrl, containerEl, infoElement) {
    infoEl = infoElement || null;
    if (!scene) {
        initScene(containerEl);
    }
    if (infoEl) infoEl.textContent = "Loading...";

    try {
        const stlResponse = await fetch(stlUrl);
        if (!stlResponse.ok) throw new Error(`model fetch failed (${stlResponse.status})`);
        const geometry = loader.parse(await stlResponse.arrayBuffer());
        showModel(geometry);

        if (supportsUrl) {
            const supportsResponse = await fetch(supportsUrl);
            if (supportsResponse.ok) {
                showSupports(await supportsResponse.json());
            }
        }
    } catch (err) {
        if (infoEl) {
            infoEl.textContent = "Couldn't load this model.";
            infoEl.classList.add("error");
        }
    }
}
