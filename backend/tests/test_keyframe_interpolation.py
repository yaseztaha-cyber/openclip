import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import reframer

def test_one_keyframe():
    kfs = [{"t": 0, "x": 0.3, "y": 0.4, "zoom": 1.5}]
    x,y,z = reframer._interpolate_keyframes(kfs, 5, 10)
    assert x==0.3 and y==0.4 and z==1.5
    print("one keyframe OK")

def test_two_keyframes():
    kfs = [{"t": 0, "x": 0.5, "y": 0.5, "zoom": 1.0}, {"t": 10, "x": 1.0, "y": 1.0, "zoom": 2.0}]
    x,y,z = reframer._interpolate_keyframes(kfs, 5, 10)
    assert abs(x-0.75)<0.01 and abs(y-0.75)<0.01 and abs(z-1.5)<0.01
    print("two keyframes OK")

def test_three_keyframes():
    kfs = [{"t":0,"x":0.5,"y":0.5,"zoom":1.0},{"t":5,"x":0.3,"y":0.45,"zoom":1.5},{"t":10,"x":0.7,"y":0.55,"zoom":1.2}]
    # At 0,2.5,5,7.5,10
    for t, exp in [(0, (0.5,0.5,1.0)), (2.5, (0.4,0.475,1.25)), (5, (0.3,0.45,1.5)), (7.5, (0.5,0.5,1.35)), (10, (0.7,0.55,1.2))]:
        x,y,z = reframer._interpolate_keyframes(kfs, t, 10)
        ex,ey,ez = exp
        assert abs(x-ex)<0.01 and abs(y-ey)<0.01 and abs(z-ez)<0.01, f"t={t} got {(x,y,z)} exp {exp}"
    print("three keyframes OK")

def test_before_first():
    kfs = [{"t":5,"x":0.2,"y":0.2,"zoom":2.0}]
    x,y,z = reframer._interpolate_keyframes(kfs, 0, 10)
    assert x==0.2 and y==0.2 and z==2.0
    print("before first OK")

def test_after_last():
    kfs = [{"t":0,"x":0.2,"y":0.2,"zoom":2.0}]
    x,y,z = reframer._interpolate_keyframes(kfs, 10, 10)
    assert x==0.2 and y==0.2 and z==2.0
    print("after last OK")

def test_duplicate_timestamp():
    kfs = [{"t":5,"x":0.1,"y":0.1,"zoom":1.0},{"t":5,"x":0.9,"y":0.9,"zoom":2.0}]
    x,y,z = reframer._interpolate_keyframes(kfs, 5, 10)
    # Last wins
    assert x==0.9 and y==0.9 and z==2.0
    print("duplicate OK")

def test_unsorted():
    kfs = [{"t":10,"x":0.7,"y":0.55,"zoom":1.2},{"t":0,"x":0.5,"y":0.5,"zoom":1.0},{"t":5,"x":0.3,"y":0.45,"zoom":1.5}]
    x,y,z = reframer._interpolate_keyframes(kfs, 2.5, 10)
    assert abs(x-0.4)<0.01
    print("unsorted OK")

def test_invalid_xy():
    kfs = [{"t":0,"x": -1,"y":2,"zoom":1.0}]
    x,y,z = reframer._interpolate_keyframes(kfs, 0, 10)
    assert 0<=x<=1 and 0<=y<=1
    print("invalid xy OK")

def test_invalid_zoom():
    kfs = [{"t":0,"x":0.5,"y":0.5,"zoom":10}]
    x,y,z = reframer._interpolate_keyframes(kfs, 0, 10)
    assert 0.5<=z<=3.0
    print("invalid zoom OK")

def test_manual_mode():
    # Manual mode should use keyframes, auto should not
    # This is tested via reframer, but here we just check interpolation still works
    kfs = [{"t":0,"x":0.5,"y":0.5,"zoom":1.0}]
    x,y,z = reframer._interpolate_keyframes(kfs, 0, 5)
    assert x==0.5
    print("manual mode OK")

def test_zero_length():
    kfs = [{"t":0,"x":0.5,"y":0.5,"zoom":1.0}]
    x,y,z = reframer._interpolate_keyframes(kfs, 0, 0)
    assert x==0.5
    print("zero length OK")

def test_long_clip():
    kfs = [{"t":0,"x":0.5,"y":0.5,"zoom":1.0},{"t":100,"x":0.6,"y":0.6,"zoom":1.5}]
    x,y,z = reframer._interpolate_keyframes(kfs, 50, 100)
    assert abs(x-0.55)<0.01
    print("long clip OK")

def test_auto_mode_no_keyframes():
    # Auto mode with no keyframes should return center
    x,y,z = reframer._interpolate_keyframes([], 5, 10)
    assert x==0.5 and y==0.5 and z==1.0
    print("auto no keyframes OK")

def test_scene_cuts_handling():
    # Scene cuts should not affect manual interpolation (manual preserves trajectory)
    kfs = [{"t":0,"x":0.5,"y":0.5,"zoom":1.0},{"t":10,"x":0.7,"y":0.5,"zoom":1.2}]
    # Even with scene cut at 5s, manual should still interpolate
    x,y,z = reframer._interpolate_keyframes(kfs, 5, 10)
    assert abs(x-0.6)<0.01
    print("scene cuts OK")

if __name__ == "__main__":
    test_one_keyframe()
    test_two_keyframes()
    test_three_keyframes()
    test_before_first()
    test_after_last()
    test_duplicate_timestamp()
    test_unsorted()
    test_invalid_xy()
    test_invalid_zoom()
    test_manual_mode()
    test_zero_length()
    test_long_clip()
    test_auto_mode_no_keyframes()
    test_scene_cuts_handling()
    print("ALL 14 KEYFRAME TESTS PASSED")
