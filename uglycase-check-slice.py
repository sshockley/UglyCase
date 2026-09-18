# -*- coding: utf-8 -*-
"""
This script splits the main body and creates TrayA/B, LidA/B, and Director
parts in a Prints group.  It also checks for various problems I've run
into with this drawing.

Ways to run it
--------------
  GUI      Macro -> Macros... -> select this file -> Execute, with the
           uglycase document open and active.
  Console  freecadcmd uglycase-check-slice.py /path/to/uglycase.FCStd

It is safe to run repeatedly.  Nothing else in the document is touched.

"""

import os
import sys

import FreeCAD as App
import Part

# One entry per printed part.
#   body   PartDesign body to read
#   whole  name for the undivided part
#   low    name for the x < split half   (omit for parts that are not split)
#   high   name for the x > split half   (omit for parts that are not split)
#   split  Parameters alias holding the split x  (omit for parts that are not
#          split -- only "whole" is produced)
PARTS = (
    {"body": "Tray", "whole": "TrayWhole",
     "low": "TrayA", "high": "TrayB", "split": "TraySplitX"},
    {"body": "Lid", "whole": "LidWhole",
     "low": "LidA", "high": "LidB", "split": "LidSplitX"},
    {"body": "Director", "whole": "DirectorWhole"},
)

REFERENCES = ("Board_BC250", "Apevia_ITX_PFC500W", "Fan_120mm")

# Folder for created parts
OUTPUT_GROUP = "Prints"

# Remove the copied whole part after split is complete
DISCARD_WHOLE_WHEN_SPLIT = True

# Tolerance in mm3
TOL = 1e-3


def _say(msg):
    """Writes out a message"""
    FreeCAD.Console.PrintMessage(str(msg) + "\n")
    # sys.stdout.write(str(msg) + "\n")
    # sys.stdout.flush()


def _param(doc, name):
    """read one value out of the Parameters spreadsheet"""
    sheet = doc.getObject("Parameters")
    if sheet is None:
        raise RuntimeError("no Parameters spreadsheet in this document")
    return float(sheet.get(name))


def _find(doc, name):
    obj = doc.getObject(name)
    if obj is not None:
        return obj
    for cand in doc.Objects:
        if getattr(cand, "Label", None) == name:
            return cand
    return None


def _solid_of(shape):
    """common() can leave a dangling face on the cut plane; keep the solid."""
    cleaned = shape.removeSplitter()
    return cleaned.Solids[0] if len(cleaned.Solids) == 1 else cleaned


def _stranded_after_tip(body):
    """checks for features after the tip"""
    tip = getattr(body, "Tip", None)
    if tip is None:
        return []
    members = [o for o in body.Group if hasattr(o, "BaseFeature")]
    found, frontier = [], [tip]
    while frontier:
        nxt = [o for o in members
               if getattr(o, "BaseFeature", None) in frontier and o not in found]
        if not nxt:
            break
        found.extend(nxt)
        frontier = nxt
    return found


def _stale_bodies(doc):
    """bodies whose cached Shape does not match their own Tip's shape"""
    out = []
    for obj in doc.Objects:
        if not obj.isDerivedFrom("PartDesign::Body"):
            continue
        tip = getattr(obj, "Tip", None)
        if tip is None or not hasattr(tip, "Shape"):
            continue
        if not obj.Shape.Faces or not tip.Shape.Faces:
            continue
        nb, nt = len(obj.Shape.Faces), len(tip.Shape.Faces)
        dv = abs(obj.Shape.Volume - tip.Shape.Volume)
        if nb != nt or dv > TOL:
            out.append("%s.Shape has %d faces / %.3f cm3 but its tip %s has "
                       "%d faces / %.3f cm3"
                       % (obj.Name, nb, obj.Shape.Volume / 1000.0,
                          tip.Name, nt, tip.Shape.Volume / 1000.0))
    return out


def _audit_tips(doc):
    """refuse to run while any body's Tip is behind its own feature chain"""
    bad = []
    for obj in doc.Objects:
        if not obj.isDerivedFrom("PartDesign::Body"):
            continue
        stranded = _stranded_after_tip(obj)
        if stranded:
            bad.append("%s.Tip is %s but %s come after it"
                       % (obj.Name, obj.Tip.Name,
                          "/".join(f.Name for f in stranded)))
    if bad:
        raise RuntimeError(
            "stale tip -- body.Shape would omit real features:\n    "
            + "\n    ".join(bad)
            + "\n  Fix: right-click the last feature -> Set tip, or\n"
              "       doc.getObject(BODY).Tip = doc.getObject(LAST); doc.recompute()")


def _output_group(doc):
    if not OUTPUT_GROUP:
        return None
    grp = doc.getObject(OUTPUT_GROUP)
    if grp is None:
        grp = doc.addObject("App::DocumentObjectGroup", OUTPUT_GROUP)
        grp.Label = OUTPUT_GROUP
    elif not grp.isDerivedFrom("App::DocumentObjectGroup"):
        raise RuntimeError("%s exists but is a %s, not a group"
                           % (OUTPUT_GROUP, grp.TypeId))
    return grp


def _ensure_in_group(obj, container):
    if container is None:
        return
    if obj not in container.Group:
        container.addObject(obj)


def _discard(doc, obj):
    """delete one derived part, unless something else depends on it"""
    users = [o.Name for o in obj.InList
             if not o.isDerivedFrom("App::DocumentObjectGroup")]
    if users:
        return "kept, still referenced by %s" % ", ".join(users)
    doc.removeObject(obj.Name)
    return "removed"


def _cleanup(doc, made):
    """drop the redundant *Whole parts of split bodies"""
    if not DISCARD_WHOLE_WHEN_SPLIT:
        return
    targets = [(n, e) for (n, e) in made.items()
               if e["low"] is not None and e["whole"] is not None]
    if not targets:
        return
    _say("")
    _say("  cleanup")
    for (_body_name, entry) in targets:
        name = entry["whole"].Name
        state = _discard(doc, entry["whole"])
        if state == "removed":
            entry["whole"] = None
        _say("    %-14s %s" % (name, state))
    doc.recompute()


def _half_spaces(bbox, split):
    """two boxes that divide space at x = split, each covering the bbox"""
    pad = 10.0
    x0, y0, z0 = bbox.XMin - pad, bbox.YMin - pad, bbox.ZMin - pad
    dy, dz = bbox.YLength + 2 * pad, bbox.ZLength + 2 * pad
    lo = Part.makeBox(split - x0, dy, dz, App.Vector(x0, y0, z0))
    hi = Part.makeBox(bbox.XMax + pad - split, dy, dz, App.Vector(split, y0, z0))
    return lo, hi


def _store(doc, name, shape, container=None):
    """create or update one Part::Feature, reporting what changed"""
    obj = doc.getObject(name)
    created = obj is None
    if created:
        obj = doc.addObject("Part::Feature", name)
        obj.Label = name
    elif not obj.isDerivedFrom("Part::Feature"):
        raise RuntimeError("%s exists but is a %s, refusing to overwrite it"
                           % (name, obj.TypeId))
    _ensure_in_group(obj, container)
    before = obj.Shape.Volume if not created and obj.Shape.Faces else 0.0
    obj.Shape = shape
    delta = shape.Volume - before
    state = "created" if created else ("unchanged" if abs(delta) < TOL
                                       else "updated %+.3f cm3" % (delta / 1000.0))
    return obj, state


def _overlap(shape, other):
    """per-solid overlap; one common() against a multi-solid compound (the PSU
    STEP is 14 solids) can report a phantom volume in OCC"""
    total = 0.0
    for solid in (other.Solids if other.Solids else [other]):
        piece = shape.common(solid)
        if piece.Volume > 1e-6:
            total += piece.Volume
    return total


def update(doc=None, check=True):
    """does the updates"""
    doc = doc or App.ActiveDocument
    if doc is None:
        raise RuntimeError("no active document")

    was_stale = _stale_bodies(doc)
    if doc.RecomputesFrozen:
        _say("  note: document had recomputes frozen -- unfreezing")
        doc.RecomputesFrozen = False
    for obj in doc.Objects:
        if obj.isDerivedFrom("PartDesign::Body"):
            obj.touch()
    doc.recompute(None, True)

    broken = [o.Name for o in doc.Objects if o.State and "Invalid" in o.State]
    if broken:
        raise RuntimeError("document has features in error, fix them first: %s"
                           % ", ".join(broken))
    _audit_tips(doc)

    still_stale = _stale_bodies(doc)
    if still_stale:
        raise RuntimeError(
            "body shape does not match its tip even after a forced "
            "recompute:\n    " + "\n    ".join(still_stale)
            + "\n  The derived parts would be cut from the wrong shape.")
    if was_stale:
        _say("  refreshed stale bodies before building:")
        for line in was_stale:
            _say("    was %s" % line)

    _say("Ugly Case -- rebuilding derived parts from %s" % doc.Name)
    group = _output_group(doc)
    made = {}

    for spec in PARTS:
        body_name = spec["body"]
        body = _find(doc, body_name)
        if body is None:
            _say("  %-8s body not found, skipped" % body_name)
            continue
        shape = body.Shape
        entry = {"body": body, "whole": None, "low": None, "high": None}
        report = []

        obj_w, st_w = _store(doc, spec["whole"], _solid_of(shape), group)
        entry["whole"] = obj_w
        report.append((spec["whole"], obj_w, st_w))

        if "split" in spec:
            split = _param(doc, spec["split"])
            lo_box, hi_box = _half_spaces(shape.BoundBox, split)
            obj_a, st_a = _store(doc, spec["low"],
                                 _solid_of(shape.common(lo_box)), group)
            obj_b, st_b = _store(doc, spec["high"],
                                 _solid_of(shape.common(hi_box)), group)
            entry["low"], entry["high"] = obj_a, obj_b
            report.append((spec["low"], obj_a, st_a))
            report.append((spec["high"], obj_b, st_b))
            _say("  %s tip %s, split at %s = %.3f"
                 % (body_name, body.Tip.Name, spec["split"], split))
        else:
            _say("  %s tip %s, not split" % (body_name, body.Tip.Name))

        made[body_name] = entry
        for (nm, ob, st) in report:
            box = ob.Shape.optimalBoundingBox(True)
            _say("    %-14s %8.3f cm3  solids %d  %6.2f x %6.2f x %6.2f  %s"
                 % (nm, ob.Shape.Volume / 1000.0, len(ob.Shape.Solids),
                    box.XLength, box.YLength, box.ZLength, st))

    doc.recompute()
    if not check:
        _cleanup(doc, made)
        return made

    _say("")
    _say("  checks")
    ok = True
    for (body_name, entry) in made.items():
        shape = entry["body"].Shape
        obj_w, obj_a, obj_b = entry["whole"], entry["low"], entry["high"]

        drift = abs(shape.Volume - obj_w.Shape.Volume)
        if obj_a is not None:
            missing = shape.cut(obj_a.Shape).cut(obj_b.Shape).Volume
            spill = obj_a.Shape.cut(shape).Volume + obj_b.Shape.cut(shape).Volume
            good = missing < TOL and spill < TOL and drift < TOL
            _say("    %-8s halves rebuild the body: missing %.4f, spill %.4f, "
                 "whole drift %.4f mm3  %s"
                 % (body_name, missing, spill, drift,
                    "OK" if good else "MISMATCH"))
        else:
            good = drift < TOL
            _say("    %-8s whole drift %.4f mm3  %s"
                 % (body_name, drift, "OK" if good else "MISMATCH"))
        ok = ok and good

        for part in (obj_a, obj_b, obj_w):
            if part is None:
                continue
            if len(part.Shape.Solids) != 1 or not part.Shape.isValid():
                ok = False
                _say("    %s is not a single valid solid" % part.Name)

    refs = [(n, _find(doc, n)) for n in REFERENCES]
    refs = [(n, o) for (n, o) in refs if o is not None]
    if refs:
        worst, worst_pair = 0.0, ""
        for entry in made.values():
            # split parts: check the halves, since those are what gets printed.
            # unsplit parts: check the whole.
            targets = ([entry["low"], entry["high"]] if entry["low"] is not None
                       else [entry["whole"]])
            for part in targets:
                for (ref_name, ref) in refs:
                    vol = _overlap(part.Shape, ref.Shape)
                    if vol > TOL:
                        _say("    %s x %s overlap %.4f mm3"
                             % (part.Name, ref_name, vol))
                    if vol > worst:
                        worst, worst_pair = vol, "%s x %s" % (part.Name, ref_name)
        _say("    worst interference vs %s: %.4f mm3  %s"
             % ("/".join(n for (n, _o) in refs), worst,
                worst_pair if worst > TOL else "OK"))
        ok = ok and worst < TOL

    _say("  %s" % ("all checks passed" if ok else "CHECKS FAILED -- see above"))

    _cleanup(doc, made)
    return made


def _document_from_args():
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".fcstd") and os.path.exists(arg):
            return App.openDocument(arg), True
    env = os.environ.get("UGLYCASE_DOC")
    if env and os.path.exists(env):
        return App.openDocument(env), True
    return App.ActiveDocument, False


def _invoked_directly():
    """freecadcmd executes a script with __name__ set to the file stem rather
    than "__main__", so the usual guard never fires there.  Treat it as a
    direct run when this file's own name appears in the command line; an
    "import uglycase-check-slice" from another script will not match."""
    if __name__ == "__main__":
        return True
    stem = os.path.splitext(os.path.basename(
        globals().get("__file__", "uglycase-check-slice.py")))[0]
    return any(os.path.splitext(os.path.basename(a))[0] == stem
               for a in sys.argv[1:])


if _invoked_directly():
    _doc, _opened = _document_from_args()
    if _doc is None:
        _say("Open the uglycase document first, or pass its path:")
        _say("  freecadcmd uglycase-check-slice.py /path/to/uglycase.FCStd")
    else:
        update(_doc)
        if _opened:
            _doc.save()
            _say("saved %s" % _doc.FileName)
        else:
            _say("done -- save the document to keep the rebuilt parts")
