using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;

public static class PixalAssetImporter {
    [Serializable] public class Files { public string fbx; public string glb; }
    [Serializable] public class Metrics { public int triangles; public int vertices; }
    [Serializable] public class Textures { public string basecolor; public string metallic_smoothness; public string normal_lod0; public string ao; public string emission; }
    [Serializable] public class Lod { public int index; public float screen_height; public Files files; public Metrics export_metrics;
        public Textures textures; public string alpha_mode; public bool emission_used; }
    [Serializable] public class Collision { public int hulls; }
    [Serializable] public class Bake { public string alpha_mode; }
    [Serializable] public class Options { public bool two_sided; }
    [Serializable] public class Manifest { public string name; public Lod[] lods; public Collision collision; public Bake bake; public Options options; }
    [Serializable] public class LodResult { public int lod; public int triangles; public int vertices; }
    [Serializable] public class Result { public string status; public string engine = "unity"; public string version;
        public string prefab; public string shader; public string reason; public int collision_hulls; public List<LodResult> lods; }

    public static void Run() {
        string output = Environment.GetEnvironmentVariable("PA_ASSET_CHECK_OUTPUT");
        try { File.WriteAllText(output, JsonUtility.ToJson(Import(), true)); }
        catch (Exception e) {
            File.WriteAllText(output, JsonUtility.ToJson(new Result {status="failed", reason=e.ToString()}, true));
            throw;
        }
    }
    static Result Import() {
        string path = Environment.GetEnvironmentVariable("PA_ASSET_MANIFEST");
        var manifest = JsonUtility.FromJson<Manifest>(File.ReadAllText(path));
        string root = Path.GetDirectoryName(path);
        string assetDir = "Assets/Pixal3D/" + manifest.name;
        Directory.CreateDirectory(assetDir);
        Directory.CreateDirectory(assetDir + "/Textures");
        string textureRoot = Path.GetFullPath(Path.Combine(root, "textures"));
        string[] sourceTextures = Directory.GetFiles(textureRoot, "*.png", SearchOption.AllDirectories);
        foreach (string source in sourceTextures) {
            string relative = source.Substring(textureRoot.Length).TrimStart(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string target = assetDir + "/Textures/" + relative.Replace('\\', '/');
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            File.Copy(source, target, true);
        }
        AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
        foreach (string source in sourceTextures) {
            string relative = source.Substring(textureRoot.Length).TrimStart(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            string target = assetDir + "/Textures/" + relative.Replace('\\', '/');
            AssetDatabase.ImportAsset(target, ImportAssetOptions.ForceSynchronousImport);
            var importer = (TextureImporter)AssetImporter.GetAtPath(target);
            string name = Path.GetFileNameWithoutExtension(source);
            importer.sRGBTexture = name == "basecolor" || name == "emission";
            if (name.StartsWith("normal_lod")) importer.textureType = TextureImporterType.NormalMap;
            importer.SaveAndReimport();
        }
        Shader shader = Shader.Find("Universal Render Pipeline/Lit") ?? Shader.Find("Standard");
        if (shader == null) throw new Exception("No supported PBR shader installed");
        bool urp = shader.name.StartsWith("Universal");
        var rootObject = new GameObject(manifest.name);
        var result = new Result { status="passed", version=Application.unityVersion, shader=shader.name,
                                  lods=new List<LodResult>(), prefab=assetDir + "/" + manifest.name + ".prefab" };
        var levels = new List<LOD>();
        Texture2D Tex(string relative) {
            if (String.IsNullOrEmpty(relative)) throw new Exception("Missing texture path in manifest");
            string suffix = relative.Replace('\\', '/').Substring("textures/".Length);
            var texture = AssetDatabase.LoadAssetAtPath<Texture2D>(assetDir + "/Textures/" + suffix);
            if (texture == null) throw new Exception("Texture failed to import: " + relative);
            return texture;
        }
        foreach (var lod in manifest.lods) {
            if (String.IsNullOrEmpty(lod.files.fbx)) throw new Exception("Unity preset requires FBX export");
            string target = assetDir + "/" + Path.GetFileName(lod.files.fbx);
            File.Copy(Path.Combine(root, lod.files.fbx), target, true);
            AssetDatabase.ImportAsset(target, ImportAssetOptions.ForceSynchronousImport);
            var importer = (ModelImporter)AssetImporter.GetAtPath(target);
            importer.importAnimation = false;
            importer.isReadable = true;
            importer.materialImportMode = ModelImporterMaterialImportMode.None;
            importer.importNormals = ModelImporterNormals.Import;
            importer.importTangents = ModelImporterTangents.Import;
            importer.SaveAndReimport();
            var model = AssetDatabase.LoadAssetAtPath<GameObject>(target);
            if (model == null) throw new Exception("Failed to import " + target);
            var instance = (GameObject)PrefabUtility.InstantiatePrefab(model);
            instance.transform.SetParent(rootObject.transform, false);
            var material = new Material(shader);
            material.SetTexture(urp ? "_BaseMap" : "_MainTex", Tex(lod.textures.basecolor));
            material.SetTexture("_MetallicGlossMap", Tex(lod.textures.metallic_smoothness));
            material.SetTexture("_BumpMap", Tex(lod.textures.normal_lod0));
            material.SetTexture("_OcclusionMap", Tex(lod.textures.ao));
            if (lod.emission_used) {
                material.SetTexture("_EmissionMap", Tex(lod.textures.emission));
                material.SetColor("_EmissionColor", Color.white);
                material.EnableKeyword("_EMISSION");
            }
            material.SetFloat("_Smoothness", 1);
            material.SetFloat("_GlossMapScale", 1);
            material.EnableKeyword("_METALLICSPECGLOSSMAP");
            material.EnableKeyword("_METALLICGLOSSMAP");
            material.EnableKeyword("_NORMALMAP");
            material.EnableKeyword("_OCCLUSIONMAP");
            if (urp) material.SetFloat("_Cull", manifest.options.two_sided ? 0 : 2);
            if (lod.alpha_mode == "mask") {
                material.SetFloat("_AlphaClip", 1);
                material.SetFloat("_Cutoff", .5f);
                material.EnableKeyword("_ALPHATEST_ON");
                material.renderQueue = 2450;
            } else if (lod.alpha_mode == "blend") {
                material.SetFloat("_Surface", 1);
                material.SetFloat("_Mode", 2);
                material.SetFloat("_SrcBlend", (float)UnityEngine.Rendering.BlendMode.SrcAlpha);
                material.SetFloat("_DstBlend", (float)UnityEngine.Rendering.BlendMode.OneMinusSrcAlpha);
                material.SetFloat("_ZWrite", 0);
                material.EnableKeyword("_SURFACE_TYPE_TRANSPARENT");
                material.EnableKeyword("_ALPHABLEND_ON");
                material.renderQueue = 3000;
            }
            string materialPath = assetDir + "/Material_LOD" + lod.index + ".mat";
            var existing = AssetDatabase.LoadAssetAtPath<Material>(materialPath);
            if (existing != null) {
                existing.shader = shader;
                existing.CopyPropertiesFromMaterial(material);
                UnityEngine.Object.DestroyImmediate(material);
                material = existing;
            } else AssetDatabase.CreateAsset(material, materialPath);
            var renderers = new List<Renderer>();
            int triangles=0, vertices=0;
            foreach (var filter in instance.GetComponentsInChildren<MeshFilter>(true)) {
                Mesh mesh = filter.sharedMesh;
                if (filter.name.StartsWith("UCX_")) {
                    var renderer = filter.GetComponent<MeshRenderer>();
                    if (renderer != null) UnityEngine.Object.DestroyImmediate(renderer);
                    var collider = filter.gameObject.AddComponent<MeshCollider>();
                    collider.sharedMesh = mesh;
                    collider.convex = true;
                    result.collision_hulls++;
                    continue;
                }
                triangles += mesh.triangles.Length / 3;
                vertices += mesh.vertexCount;
                if (mesh.normals.Length != mesh.vertexCount || mesh.uv.Length != mesh.vertexCount ||
                    mesh.tangents.Length != mesh.vertexCount) throw new Exception("Missing normals/UV/tangents");
                var mr = filter.GetComponent<MeshRenderer>();
                mr.sharedMaterial = material;
                renderers.Add(mr);
            }
            if (triangles != lod.export_metrics.triangles) throw new Exception("Triangle mismatch at LOD" + lod.index);
            result.lods.Add(new LodResult {lod=lod.index, triangles=triangles, vertices=vertices});
            levels.Add(new LOD(lod.screen_height, renderers.ToArray()));
        }
        var group = rootObject.AddComponent<LODGroup>();
        group.SetLODs(levels.ToArray());
        group.RecalculateBounds();
        PrefabUtility.SaveAsPrefabAsset(rootObject, result.prefab);
        UnityEngine.Object.DestroyImmediate(rootObject);
        AssetDatabase.SaveAssets();
        if (result.collision_hulls != manifest.collision.hulls) throw new Exception("Collision hull count mismatch");
        return result;
    }
}
