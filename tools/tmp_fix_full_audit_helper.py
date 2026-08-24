from pathlib import Path

path = Path(__file__).with_name("tmp_apply_full_audit_remediation.py")
text = path.read_text()

old = '''dispatch = replace_once(
    dispatch,
    "                f\\\"size variance {delta_bytes} bytes ({delta_percent:.4f}%)\\\"\\n",
    "                f\\\"size variance {delta_bytes} bytes ({delta_percent:.4f}%); \\\"\\n                + (\\\"sample fingerprint matched\\\" if duplicate.get(\\\"_mirror_sample_verified\\\") else \\\"exact provider size matched\\\")\\n",
    label="mirror evidence reason",
)
'''
new = '''old_reason = ''' + "'''" + '''            reason = (\n                f"Duplicate mirror of {primary_host}; matching resolved filename; "\n                f"size variance {delta_bytes} bytes ({delta_percent:.4f}%)"\n                if primary_host\n                else (\n                    "Duplicate cross-hoster mirror; matching resolved filename; "\n                    f"size variance {delta_bytes} bytes ({delta_percent:.4f}%)"\n                )\n            )\n''' + "'''" + '''\nnew_reason = ''' + "'''" + '''            evidence = (\n                "sample fingerprint matched"\n                if duplicate.get("_mirror_sample_verified")\n                else "exact provider size matched"\n            )\n            reason = (\n                f"Duplicate mirror of {primary_host}; matching resolved filename; "\n                f"size variance {delta_bytes} bytes ({delta_percent:.4f}%); {evidence}"\n                if primary_host\n                else (\n                    "Duplicate cross-hoster mirror; matching resolved filename; "\n                    f"size variance {delta_bytes} bytes ({delta_percent:.4f}%); {evidence}"\n                )\n            )\n''' + "'''" + '''\ndispatch = replace_once(\n    dispatch, old_reason, new_reason, label="mirror evidence reason"\n)\n'''

if old not in text:
    raise RuntimeError("Could not locate mirror evidence helper block")
text = text.replace(old, new, 1)

# Keep the already validated relative tolerance and 512 MiB catastrophe guard.
# The audit remediation strengthens the tolerance path with content samples
# instead of shrinking the candidate window underneath existing behavior.
text = text.replace(
    "_MAX_MIRROR_SIZE_DELTA_BYTES = 4 * 1024 * 1024",
    "_MAX_MIRROR_SIZE_DELTA_BYTES = 512 * 1024 * 1024",
)
text = text.replace("4 MiB", "512 MiB")

# Historical upstream tags already occupy v1.0.7-v1.0.9 in this repository.
# Use the next available patch identity rather than rewriting inherited tags.
text = text.replace("1.0.7", "1.0.10")

path.write_text(text)
print("Audit remediation helper corrected")
