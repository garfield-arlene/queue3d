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

const BED_WIDTH_MM = 295;
const BED_DEPTH_MM = 195;
const BED_HEIGHT_MM = 165;

const loader = new STLLoader();
let scene, camera, renderer, controls, container, infoEl;
let currentMesh = null;
let currentSupports = null;
let resizeObserver = null;
// The model exactly as loaded/parsed, before any scaling - kept around so
// setPreviewScale()/autoFitScale() below always compute from the true
// original size, not whatever scale happened to be applied last. Cloned
// fresh into renderGeometry() on every (re)scale rather than mutated in
// place, so repeated scale changes never compound.
let rawGeometry = null;

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
 * Renders `geometry` (already scaled by the caller, if at all) - the
 * shared body behind both showModel() (a fresh load) and
 * setPreviewScale() (re-rendering the same raw model at a new scale,
 * with no re-fetch/re-parse needed).
 */
function renderGeometry(geometry) {
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

    geometry.computeVertexNormals();

    // Center the model on the bed in X/Y and drop it so its lowest point
    // sits on the plate (Z=0) - the same "auto place on bed" a slicer does.
    // Area-weighted surface centroid, not the bounding-box midpoint - see
    // surfaceCentroidXY()'s own comment and slicing/stl_to_3mf.py's
    // matching center_vertices(): an asymmetric model (most of its
    // surface well off from its own box's middle) can pass bbox-centering
    // fine here but then fail the slicer's own downstream bed-centering
    // check, since that check looks at where the sliced material actually
    // ends up, not the box. Confirmed against a real model that failed
    // exactly that way before this changed.
    const centroid = surfaceCentroidXY(geometry);
    geometry.translate(-centroid.x, -centroid.y, -box.min.z);

    const overBuildVolume = size.x > BED_WIDTH_MM || size.y > BED_DEPTH_MM || size.z > BED_HEIGHT_MM;

    const material = new THREE.MeshPhongMaterial({
        color: overBuildVolume ? 0xcc4433 : 0x3a9ad9,
        specular: 0x111111,
        shininess: 40,
    });

    currentMesh = new THREE.Mesh(geometry, material);
    scene.add(currentMesh);

    // Frame the camera around the model - distance scales with its size so
    // a tiny calibration cube and a bed-filling model both start visible.
    // near/far scale too: a fixed far plane would clip (render nothing for)
    // a model large enough to need a camera distance beyond it - a real
    // risk here specifically, since "too large for the build plate" is
    // often a units mistake (e.g. meters exported as if mm) rather than a
    // merely-somewhat-oversized model, and can be 1000x larger than expected.
    const radius = Math.max(size.x, size.y, size.z, 20);
    camera.near = Math.max(radius / 1000, 0.01);
    camera.far = radius * 20;
    camera.updateProjectionMatrix();
    camera.position.set(radius * 1.4, -radius * 1.6, radius * 1.3);
    controls.target.set(0, 0, size.z / 2);
    controls.update();

    if (infoEl) {
        const dims = `${size.x.toFixed(1)} x ${size.y.toFixed(1)} x ${size.z.toFixed(1)} mm`;
        infoEl.textContent = overBuildVolume
            ? `${dims} - too large for the build plate (${BED_WIDTH_MM} x ${BED_DEPTH_MM} x ${BED_HEIGHT_MM} mm)`
            : dims;
        infoEl.classList.toggle("error", overBuildVolume);
    }
}

function showModel(geometry) {
    rawGeometry = geometry.clone();
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
export function applyTransform({ scale = 1, rotateX = 0, rotateY = 0, rotateZ = 0 } = {}) {
    if (!rawGeometry) return;
    const geometry = rawGeometry.clone();
    if (rotateX) geometry.rotateX(THREE.MathUtils.degToRad(rotateX));
    if (rotateY) geometry.rotateY(THREE.MathUtils.degToRad(rotateY));
    if (rotateZ) geometry.rotateZ(THREE.MathUtils.degToRad(rotateZ));
    if (scale !== 1) geometry.scale(scale, scale, scale);
    renderGeometry(geometry);
}

/**
 * The largest scale factor (capped at 1 - this only ever shrinks, never
 * grows a model that already fits) that would bring the model's
 * bounding box within the build plate on all three axes, *at the given
 * rotation* - rotating changes the footprint, so a model already
 * reoriented (by hand, or via "snap to surface") needs auto-fit
 * computed against that orientation's actual bounding box, not the
 * as-uploaded one. Returns 1 if nothing's loaded, or if it already
 * fits.
 */
export function autoFitScale(rotateX = 0, rotateY = 0, rotateZ = 0) {
    if (!rawGeometry) return 1;
    const geometry = rawGeometry.clone();
    if (rotateX) geometry.rotateX(THREE.MathUtils.degToRad(rotateX));
    if (rotateY) geometry.rotateY(THREE.MathUtils.degToRad(rotateY));
    if (rotateZ) geometry.rotateZ(THREE.MathUtils.degToRad(rotateZ));
    geometry.computeBoundingBox();
    const size = new THREE.Vector3();
    geometry.boundingBox.getSize(size);
    if (![size.x, size.y, size.z].every(Number.isFinite)) return 1;
    return Math.min(1, BED_WIDTH_MM / size.x, BED_DEPTH_MM / size.y, BED_HEIGHT_MM / size.z);
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

    const rect = renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(
        ((clientX - rect.left) / rect.width) * 2 - 1,
        -((clientY - rect.top) / rect.height) * 2 + 1
    );
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(ndc, camera);
    const hits = raycaster.intersectObject(currentMesh, false);
    if (hits.length === 0 || !hits[0].face) return null;

    // currentMesh's own Object3D transform is always identity (every
    // rotation/scale/centering is baked directly into its geometry's
    // vertex data - see renderGeometry above) - so the hit face's normal
    // is already in the same space the rest of this function works in,
    // no extra transform needed.
    const clickedNormal = hits[0].face.normal.clone().normalize();

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

const SUPPORT_TUBE_RADIUS = 0.3; // mm - roughly a support strand's width

function showSupports(segments) {
    if (currentSupports) {
        scene.remove(currentSupports);
        currentSupports.geometry.dispose();
        currentSupports.material.dispose();
        currentSupports = null;
    }
    if (!segments || segments.length === 0) return;

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
