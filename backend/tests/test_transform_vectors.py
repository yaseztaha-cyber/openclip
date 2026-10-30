import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import reframer

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tests", "fixtures", "transform_vectors.json")

def test_python_vectors():
    data = json.load(open(FIXTURE))
    vectors = data["vectors"]
    failed = 0
    for v in vectors:
        w2,h2,x2,y2 = reframer._compute_crop(v["source_width"], v["source_height"], v["zoom"], v["x"]*v["source_width"], v["y"]*v["source_height"], v["output_width"], v["output_height"], 0, 0)
        # Our fixture uses direct ar, so compare
        # The fixture's expected is from direct compute, so we compare to that
        if (w2,h2,x2,y2) != (v["crop_width"], v["crop_height"], v["crop_x"], v["crop_y"]):
            failed += 1
            print(f"FAIL {v} -> got {(w2,h2,x2,y2)}")
    assert failed==0, f"{failed}/{len(vectors)} vectors failed"
    print(f"Python vectors {len(vectors)}/{len(vectors)} passed")

def test_python_bounds():
    data = json.load(open(FIXTURE))
    for v in data["vectors"]:
        w,h,x,y = v["crop_width"], v["crop_height"], v["crop_x"], v["crop_y"]
        assert 0 <= x <= v["source_width"]-w, f"x out of bounds {v}"
        assert 0 <= y <= v["source_height"]-h, f"y out of bounds {v}"
        assert abs(w/h - v["output_width"]/v["output_height"]) < 0.01, f"aspect wrong {v}"

def test_interpolation_vectors():
    data = json.load(open(FIXTURE))
    for iv in data["interpolation"]:
        t, x, y, zoom = iv["t"], iv["x"], iv["y"], iv["zoom"]
        # Recompute via reframer
        kfs = [{"t":0,"x":0.5,"y":0.5,"zoom":1.0},{"t":5,"x":0.3,"y":0.45,"zoom":1.5},{"t":10,"x":0.7,"y":0.55,"zoom":1.2}]
        rx, ry, rz = reframer._interpolate_keyframes(kfs, t, 10)
        assert abs(rx - x) < 0.01 and abs(ry - y) < 0.01 and abs(rz - zoom) < 0.01, f"interp t={t} got {(rx,ry,rz)} exp {(x,y,zoom)}"
    print("Interpolation vectors passed")

if __name__ == "__main__":
    test_python_vectors()
    test_python_bounds()
    test_interpolation_vectors()
    print("ALL PYTHON VECTOR TESTS PASSED")
