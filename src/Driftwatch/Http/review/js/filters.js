/** Filter state for the finding list. */

export function createFilters() {
  const state = { query: "", severity: "all", label: "all" };

  function bind(form, onChange) {
    const search = form.querySelector("#search");
    const severity = form.querySelector("#severity");
    const label = form.querySelector("#label");
    const update = () => {
      state.query = search.value.trim().toLowerCase();
      state.severity = severity.value;
      state.label = label.value;
      onChange();
    };
    search.addEventListener("input", update);
    severity.addEventListener("change", update);
    label.addEventListener("change", update);
  }

  function apply(findings) {
    return findings.filter((finding) => {
      if (state.severity !== "all" && finding.severity !== state.severity) {
        return false;
      }
      if (state.label === "unlabeled") {
        if (finding.verdict) return false;
      } else if (state.label !== "all" && finding.verdict !== state.label) {
        return false;
      }
      if (!state.query) return true;
      const haystack = [
        finding.repo,
        finding.file_path,
        finding.change_type,
        finding.surface_ref,
        finding.snippet,
        finding.detail,
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(state.query);
    });
  }

  return { bind, apply };
}
