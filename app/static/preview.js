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
    }

    geometry.computeBoundingBox();
    geometry.computeVertexNormals();
    const box = geometry.boundingBox;
    const size = new THREE.Vector3();
    box.getSize(size);

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
    const radius = Math.max(size.x, size.y, size.z, 20);
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
