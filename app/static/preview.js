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
    resizeToContainer();

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

function showModel(geometry) {
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
    const center = new THREE.Vector3();
    box.getCenter(center);
    geometry.translate(-center.x, -center.y, -box.min.z);

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
