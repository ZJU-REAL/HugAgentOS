"""Hash-guarded native-runtime patches; Docker runtime behavior is untouched."""
import argparse
import hashlib
from pathlib import Path

ORIGINAL = {
    "server-volumes": ("opensandbox_server/services/k8s/volume_helper.py", "4eb95e848454e1b7a5dc3685c57b271dbed006b4ad464badc10a59acd5452dc8"),
    "committer-security": ("kubernetes/cmd/image-committer/main.go", "eaf884ddfc989bfaaf78069650b8abd25ad5ca5418260ebb11746f2472c74c15"),
    "snapshot-trust": ("kubernetes/internal/controller/sandboxsnapshot_lifecycle.go", "a0ac9c2f4d9793cf85a572ed5dd21d0e7e02b1ad39611591a8d80d362c4f0427"),
    "controller": ("kubernetes/cmd/controller/main.go", "b3d01d62c2a8e73056bceac4266e78ac41b81e64baa9b646bf0efcd2eff23dc7"),
    "server-security": ("opensandbox_server/services/k8s/security_context.py", "91a87c7d74a02dec2bce0b40bacdf454a67435a6e3c5a920cf1161850587c83f"),
    "server": ("opensandbox_server/services/k8s/provider_common.py", "e90a2e50f5592104313fa0c4be103d85f5ed96baeed53ce070b669fac5e1223e"),
}



def patch_identity():
    root = Path(__file__).resolve().parent
    return hashlib.sha256((root / "patch_upstream.py").read_bytes()
                          + (root / "space_mounts.py").read_bytes()).hexdigest()


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError("Pinned source patch anchor changed")
    return text.replace(before, after)


def patched(text, component):
    if hashlib.sha256(text.encode()).hexdigest() != ORIGINAL[component][1]:
        raise ValueError("Pinned upstream source hash changed: " + component)
    if component == "server-volumes":
        text = replace_once(text, '    containers = pod_spec.get("containers", [])', '    from opensandbox_server.services.k8s.space_mounts import apply_object_volumes\n    volumes = apply_object_volumes(pod_spec, volumes)\n    containers = pod_spec.get("containers", [])')
    elif component == "controller":
        text = replace_once(text, 'ctrl "sigs.k8s.io/controller-runtime"', 'ctrl "sigs.k8s.io/controller-runtime"\n\t"sigs.k8s.io/controller-runtime/pkg/cache"')
        text = replace_once(text, '\tmgr, err := ctrl.NewManager(config, ctrl.Options{', '\tworkNamespace := os.Getenv("HUGAGENT_SANDBOX_NAMESPACE")\n\tif workNamespace == "" {\n\t\tsetupLog.Error(fmt.Errorf("missing namespace"), "HUGAGENT_SANDBOX_NAMESPACE is required")\n\t\tos.Exit(1)\n\t}\n\tmgr, err := ctrl.NewManager(config, ctrl.Options{\n\t\tCache: cache.Options{DefaultNamespaces: map[string]cache.Config{workNamespace: {}}},\n\t\tLeaderElectionNamespace: os.Getenv("POD_NAMESPACE"),')
        text = replace_once(text, 'LeaderElectionID:       "2fa1c467.opensandbox.io"', 'LeaderElectionID:       workNamespace + ".opensandbox.io"')
        text = replace_once(text, 'profileStore.SetupWithManager(mgr, os.Getenv("POD_NAMESPACE"))', 'profileStore.SetupWithManager(mgr, workNamespace)')
    elif component == "snapshot-trust":
        text = replace_once(text, '\tif r.SnapshotPushSecret != "" {', """\tvolumes = append(volumes, corev1.Volume{
        Name: "registry-trust",
        VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
            LocalObjectReference: corev1.LocalObjectReference{Name: "registry-trust"},
        }},
    })
    volumeMounts = append(volumeMounts, corev1.VolumeMount{
        Name: "registry-trust", MountPath: "/var/run/hugagent/registry-trust", ReadOnly: true,
    })
    if r.SnapshotPushSecret != "" {""")
        text = replace_once(text,
            'env := []corev1.EnvVar{{Name: "CONTAINERD_SOCKET", Value: ContainerdSocketPath}}',
            'env := []corev1.EnvVar{{Name: "CONTAINERD_SOCKET", Value: ContainerdSocketPath}, {Name: "SSL_CERT_FILE", Value: "/var/run/hugagent/registry-trust/ca.crt"}}')
        text = replace_once(text,
            '\tif r.SnapshotRegistryInsecure {\n\t\tenv = append(env, corev1.EnvVar{Name: "SNAPSHOT_REGISTRY_INSECURE", Value: "true"})\n\t}',
            '\tenv = append(env, corev1.EnvVar{Name: "SNAPSHOT_REGISTRY_INSECURE", Value: fmt.Sprintf("%t", r.SnapshotRegistryInsecure)})')
    elif component == "committer-security":
        text = replace_once(text, 'nerdctlBaseArgs(), "login", "-u", username, "-p", password',
                            'nerdctlBaseArgs(), "login", "-u", username, "--password-stdin"')
        text = replace_once(text, 'cmd := exec.Command("nerdctl", loginOpts...)',
                            'cmd := exec.Command("nerdctl", loginOpts...)\n\tcmd.Stdin = strings.NewReader(password)')
    elif component == "server-security":
        text = replace_once(text, "from kubernetes.client import V1SecurityContext, V1Capabilities", "from kubernetes.client import V1SecurityContext, V1Capabilities, V1SeccompProfile")
        text = replace_once(text, "        privileged=privileged,", "        privileged=privileged,\n        allow_privilege_escalation=security_context_dict.get('allowPrivilegeEscalation'),\n        seccomp_profile=V1SeccompProfile(**security_context_dict['seccompProfile']) if 'seccompProfile' in security_context_dict else None,")
        text = replace_once(text, "    return result if result else None", "    if security_context.allow_privilege_escalation is not None:\n        result['allowPrivilegeEscalation'] = security_context.allow_privilege_escalation\n    if security_context.seccomp_profile is not None:\n        result['seccompProfile'] = {'type': security_context.seccomp_profile.type}\n    return result if result else None")
    else:
        before = "    security_context = None\n    if has_network_policy:\n        security_context_dict = build_security_context_for_sandbox_container(True)\n        security_context = build_security_context_from_dict(security_context_dict)"
        after = """    # execd changes uid/gid and owns workspace files; retain only those
    # runtime capabilities. User command children run as the requested uid.
    security_context = build_security_context_from_dict({
        "privileged": False,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"], "add": [
            "CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "KILL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    })"""
        text = replace_once(text, before, after)
    return text


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--component", choices=ORIGINAL, required=True)
    a = p.parse_args()
    path = a.root / ORIGINAL[a.component][0]
    path.write_text(patched(path.read_text(), a.component))
