import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import {
  CSS2DObject,
  CSS2DRenderer,
} from 'three/examples/jsm/renderers/CSS2DRenderer.js'
import type { Annotation, ModelViewHandle } from './types'
import { cameraFitDistance, normalizedClipConstant } from './geometry'

interface ModelCanvasProps {
  /** Presigned S3 URL to the STL mesh. */
  stlUrl: string
  /** Render the mesh as a wireframe (Req 6.2). */
  wireframe: boolean
  /**
   * Cross-section slider value in [0, 1]. `0.5` centres the plane; other values
   * slide it toward either extreme of the model (Req 6.2). Enabled only when
   * {@link clipEnabled} is true.
   */
  clipOffset: number
  clipEnabled: boolean
  /** Show the XYZ axes helper (toolbar toggle). */
  axes?: boolean
  /** Engineering annotations overlaid as clickable CSS2D labels (Req 6.4). */
  annotations: readonly Annotation[]
  /** Register this canvas's imperative controls with the parent toolbar. */
  registerHandle?: (handle: ModelViewHandle) => void
  /** Remove this canvas's controls from the parent toolbar on unmount. */
  unregisterHandle?: (handle: ModelViewHandle) => void
}

/**
 * Whether a WebGL context can be created in the current environment. Guards
 * against import/build-time crashes and headless (jsdom) rendering where no
 * canvas/WebGL is available.
 */
function isWebGLAvailable(): boolean {
  if (typeof document === 'undefined') return false
  try {
    const canvas = document.createElement('canvas')
    return Boolean(
      window.WebGLRenderingContext &&
        (canvas.getContext('webgl') || canvas.getContext('experimental-webgl')),
    )
  } catch {
    return false
  }
}

/** Build the clickable, expandable DOM element for a single annotation (Req 6.4). */
function createAnnotationElement(annotation: Annotation): HTMLDivElement {
  const kindClasses: Record<Annotation['kind'], string> = {
    dimension: 'border-sky-400 bg-sky-50 text-sky-900',
    gdt: 'border-violet-400 bg-violet-50 text-violet-900',
    note: 'border-amber-400 bg-amber-50 text-amber-900',
  }

  const wrapper = document.createElement('div')
  wrapper.className = 'pointer-events-auto select-none'

  const button = document.createElement('button')
  button.type = 'button'
  button.className =
    'cursor-pointer rounded-md border px-2 py-0.5 text-xs font-semibold shadow-sm ' +
    kindClasses[annotation.kind]
  button.textContent = annotation.label

  const detail = document.createElement('div')
  detail.className =
    'mt-1 hidden max-w-[16rem] rounded-md border border-slate-300 bg-slate-50 p-2 text-left text-[11px] leading-snug text-slate-700 shadow-md'

  const rows: string[] = []
  const d = annotation.detail
  if (d.value) rows.push(`Value: ${d.value}`)
  if (d.tolerance) rows.push(`Tolerance: ${d.tolerance}`)
  if (d.datumRefs && d.datumRefs.length > 0) rows.push(`Datums: ${d.datumRefs.join(', ')}`)
  if (d.text) rows.push(d.text)
  detail.textContent = rows.length > 0 ? rows.join(' · ') : 'No additional detail.'

  button.addEventListener('click', () => {
    detail.classList.toggle('hidden')
  })

  wrapper.appendChild(button)
  wrapper.appendChild(detail)
  return wrapper
}

/**
 * Imperative Three.js renderer for a single STL model (design §6). Uses
 * OrbitControls (rotate/zoom/pan), STLLoader for the mesh, CSS2DRenderer for
 * annotation labels, and a clipping plane for cross-sections. All GPU/DOM
 * resources are disposed and the animation frame cancelled on unmount.
 *
 * Chosen over react-three-fiber so scene lifecycle, clipping planes, and the
 * CSS2D overlay are explicit and the surrounding panel stays fully typed and
 * buildable with the installed `three` / `@types/three` versions.
 */
function ModelCanvas({
  stlUrl,
  wireframe,
  clipOffset,
  clipEnabled,
  axes = false,
  annotations,
  registerHandle,
  unregisterHandle,
}: ModelCanvasProps) {
  const mountRef = useRef<HTMLDivElement | null>(null)
  // Mutable handles shared across effects without triggering re-renders.
  const materialRef = useRef<THREE.MeshPhongMaterial | null>(null)
  const planeRef = useRef<THREE.Plane | null>(null)
  const radiusRef = useRef<number>(1)

  // Scene-object refs so the imperative control handle (camera presets, reset,
  // screenshot) can act on the live scene without rebuilding it.
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const controlsRef = useRef<OrbitControls | null>(null)
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null)
  const sceneRef = useRef<THREE.Scene | null>(null)
  const axesRef = useRef<THREE.AxesHelper | null>(null)

  // "Latest value" refs so the async scene-setup effect can read the current
  // control state at load time without depending on (and rebuilding for) them.
  const wireframeRef = useRef(wireframe)
  const clipEnabledRef = useRef(clipEnabled)
  const clipOffsetRef = useRef(clipOffset)
  const axesInitRef = useRef(axes)
  wireframeRef.current = wireframe
  clipEnabledRef.current = clipEnabled
  clipOffsetRef.current = clipOffset

  // Stable imperative handle: its methods read the scene-object refs, so the
  // object identity never changes and always operates on the current scene.
  const apiRef = useRef<ModelViewHandle>({
    resetView() {
      const camera = cameraRef.current
      const controls = controlsRef.current
      if (!camera || !controls) return
      const distance = cameraFitDistance(radiusRef.current, camera.fov) || 100
      camera.up.set(0, 1, 0)
      camera.position.set(distance * 0.6, distance * 0.45, distance * 0.75)
      controls.target.set(0, 0, 0)
      controls.update()
    },
    setView(view) {
      const camera = cameraRef.current
      const controls = controlsRef.current
      if (!camera || !controls) return
      const distance = cameraFitDistance(radiusRef.current, camera.fov) || 100
      if (view === 'front') {
        camera.up.set(0, 1, 0)
        camera.position.set(0, 0, distance)
      } else if (view === 'top') {
        camera.up.set(0, 0, -1)
        camera.position.set(0, distance, 0)
      } else {
        camera.up.set(0, 1, 0)
        camera.position.set(distance, 0, 0)
      }
      controls.target.set(0, 0, 0)
      controls.update()
    },
    screenshot(filename) {
      const renderer = rendererRef.current
      const scene = sceneRef.current
      const camera = cameraRef.current
      if (!renderer || !scene || !camera) return
      renderer.render(scene, camera)
      const dataUrl = renderer.domElement.toDataURL('image/png')
      const link = document.createElement('a')
      link.download = filename
      link.href = dataUrl
      link.click()
    },
  })

  // Register/unregister this canvas's controls with the parent toolbar.
  useEffect(() => {
    const handle = apiRef.current
    registerHandle?.(handle)
    return () => unregisterHandle?.(handle)
  }, [registerHandle, unregisterHandle])

  // Scene setup + STL load. Re-runs only when the model or annotations change.
  useEffect(() => {
    const mount = mountRef.current
    if (!mount || !isWebGLAvailable()) return

    const width = mount.clientWidth || 1
    const height = mount.clientHeight || 1

    const scene = new THREE.Scene()
    // Match the 3D viewport background to the active display scheme (reads the
    // themed page-background CSS variable) so a dark theme doesn't leave a
    // jarring light canvas. Falls back to the light default when unset.
    const themedBg = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-slate-100')
      .trim()
    scene.background = new THREE.Color(themedBg || '#f8fafc')

    const camera = new THREE.PerspectiveCamera(50, width / height, 0.1, 5000)
    camera.position.set(0, 0, 100)

    // preserveDrawingBuffer lets the toolbar capture a PNG screenshot via
    // toDataURL after a render.
    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      preserveDrawingBuffer: true,
    })
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setSize(width, height)
    renderer.localClippingEnabled = true
    mount.appendChild(renderer.domElement)

    // Expose the scene objects to the imperative control handle.
    sceneRef.current = scene
    cameraRef.current = camera
    rendererRef.current = renderer

    const labelRenderer = new CSS2DRenderer()
    labelRenderer.setSize(width, height)
    labelRenderer.domElement.style.position = 'absolute'
    labelRenderer.domElement.style.top = '0'
    labelRenderer.domElement.style.left = '0'
    labelRenderer.domElement.style.pointerEvents = 'none'
    mount.appendChild(labelRenderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.enablePan = true
    controls.enableZoom = true
    controls.enableRotate = true
    controlsRef.current = controls

    scene.add(new THREE.AmbientLight(0xffffff, 0.7))
    const keyLight = new THREE.DirectionalLight(0xffffff, 0.8)
    keyLight.position.set(1, 1, 1)
    scene.add(keyLight)

    // Reference grid + toggleable XYZ axes (parity with the original viewer).
    const grid = new THREE.GridHelper(200, 20, 0x64748b, 0x334155)
    ;(grid.material as THREE.Material).opacity = 0.35
    ;(grid.material as THREE.Material).transparent = true
    grid.position.y = -0.001
    scene.add(grid)

    const axesHelper = new THREE.AxesHelper(50)
    axesHelper.visible = axesInitRef.current
    scene.add(axesHelper)
    axesRef.current = axesHelper

    const plane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0)
    planeRef.current = plane

    let mesh: THREE.Mesh | null = null
    let geometry: THREE.BufferGeometry | null = null
    let material: THREE.MeshPhongMaterial | null = null
    let disposed = false

    const loader = new STLLoader()
    loader.load(
      stlUrl,
      (loaded: THREE.BufferGeometry) => {
        if (disposed) {
          loaded.dispose()
          return
        }
        geometry = loaded
        geometry.center()
        geometry.computeVertexNormals()
        geometry.computeBoundingSphere()
        const radius = geometry.boundingSphere?.radius ?? 1
        radiusRef.current = radius

        material = new THREE.MeshPhongMaterial({
          color: 0x2563eb,
          specular: 0x111111,
          shininess: 40,
          side: THREE.DoubleSide,
          wireframe: wireframeRef.current,
          clippingPlanes: clipEnabledRef.current ? [plane] : [],
          clipShadows: true,
        })
        materialRef.current = material

        mesh = new THREE.Mesh(geometry, material)
        scene.add(mesh)

        // Anchor annotation labels relative to the (centred) model.
        for (const annotation of annotations) {
          const el = createAnnotationElement(annotation)
          const label = new CSS2DObject(el)
          label.position.set(...annotation.position)
          mesh.add(label)
        }

        // Frame the model in a default 3/4 view so it reads as 3D (Req 6.2).
        const distance = cameraFitDistance(radius, camera.fov) || 100
        camera.position.set(distance * 0.6, distance * 0.45, distance * 0.75)
        camera.near = Math.max(distance / 100, 0.01)
        camera.far = distance * 100 || 5000
        camera.updateProjectionMatrix()
        controls.target.set(0, 0, 0)
        controls.update()
        plane.constant = normalizedClipConstant(clipOffsetRef.current, radius)
      },
      undefined,
      () => {
        // Swallow load errors here; the panel surfaces empty/error state itself.
      },
    )

    let frameId = 0
    const animate = () => {
      frameId = requestAnimationFrame(animate)
      controls.update()
      renderer.render(scene, camera)
      labelRenderer.render(scene, camera)
    }
    animate()

    const handleResize = () => {
      const w = mount.clientWidth || 1
      const h = mount.clientHeight || 1
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
      labelRenderer.setSize(w, h)
    }
    const resizeObserver = new ResizeObserver(handleResize)
    resizeObserver.observe(mount)

    return () => {
      disposed = true
      cancelAnimationFrame(frameId)
      resizeObserver.disconnect()
      controls.dispose()
      if (mesh) {
        mesh.clear()
        scene.remove(mesh)
      }
      geometry?.dispose()
      material?.dispose()
      renderer.dispose()
      materialRef.current = null
      planeRef.current = null
      cameraRef.current = null
      controlsRef.current = null
      rendererRef.current = null
      sceneRef.current = null
      axesRef.current = null
      if (renderer.domElement.parentNode === mount) {
        mount.removeChild(renderer.domElement)
      }
      if (labelRenderer.domElement.parentNode === mount) {
        mount.removeChild(labelRenderer.domElement)
      }
    }
  }, [stlUrl, annotations])

  // Live wireframe toggle without rebuilding the scene (Req 6.2).
  useEffect(() => {
    if (materialRef.current) {
      materialRef.current.wireframe = wireframe
    }
  }, [wireframe])

  // Live cross-section: enable/disable the clip plane and slide it (Req 6.2).
  useEffect(() => {
    const material = materialRef.current
    const plane = planeRef.current
    if (!material || !plane) return
    material.clippingPlanes = clipEnabled ? [plane] : []
    material.needsUpdate = true
    plane.constant = normalizedClipConstant(clipOffset, radiusRef.current)
  }, [clipEnabled, clipOffset])

  // Live axes-helper visibility toggle.
  useEffect(() => {
    axesInitRef.current = axes
    if (axesRef.current) {
      axesRef.current.visible = axes
    }
  }, [axes])

  return <div ref={mountRef} className="relative h-full w-full overflow-hidden" />
}

export default ModelCanvas
