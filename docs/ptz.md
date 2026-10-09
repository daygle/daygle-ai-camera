# PTZ control and auto-tracking

PTZ cameras can be driven from the Live page and can follow a chosen kind of
object on their own. Turn PTZ on in **Cameras → Edit Camera → PTZ**.

## Connection

- **Protocol**
  - **ONVIF** (recommended) talks to the camera's web (ONVIF) port with the
    username and password from the Connection tab.
  - **TCP Pelco-D** sends raw Pelco-D frames to the Command Port, for older
    cameras without ONVIF.
- **HTTP Port** - the ONVIF port, usually 80.
- **Speed** (1-8) and **Step Duration** set how fast and how long each press of
  the Live page pad moves the camera.

ONVIF endpoints differ by vendor (`/onvif/ptz_service`, `/onvif/PTZ`, ...).
The app asks the camera for its real service paths once (GetCapabilities), and
uses the **path** it reports with the host and port you configured, so a camera
behind NAT or a port forward still works. If the camera does not answer that
request, the common default paths are used. It also picks the media profile
that is actually bound to PTZ, rather than simply the first profile.

Service paths and the profile are cached for an hour. If the camera reboots or
its profiles change, the next command gets a SOAP fault, the cache is dropped
and the command is retried once.

### Responsiveness

- Commands to one camera are sent one at a time, in order. While a pad button
  is held, a repeat move is skipped if the previous one has not finished, and
  the release's Stop always goes out last. This prevents the camera lagging
  behind the button and then drifting.
- Opening the Live page on a PTZ camera warms the ONVIF caches, so the first
  press does not wait for discovery.
- While a button is held, the live picture refreshes about every 150 ms
  instead of every 500 ms. It still cannot update faster than the camera's
  detection stream (**Detection Frame Rate**, default 4 fps).
- Pelco-D speed now uses the protocol's full 0-63 range. Previously the 1-8
  setting was sent unscaled, so the default speed 5 moved at about 8% of full
  speed.

If moves still feel slow on an ONVIF camera, raise **Speed**. If the camera
moves in short jerks while a button is held, raise **Step Duration** so each
move lasts longer than the round trip to the camera.

## Auto-tracking

The **Auto-Tracking** section of the PTZ tab keeps a detected object in the
middle of the picture.

| Setting | What it does | Default |
| --- | --- | --- |
| Follow These Objects | Comma-separated labels, e.g. `cat`. Groups such as `animal` or `pet` follow any member, so a cat misread as a dog at night is still followed. | `person` |
| Tracking Speed | How fast the camera turns towards the object (1-8). | 4 |
| Dead Zone (%) | How far from the centre the object may drift before the camera moves. | 15 |
| Lost After (s) | How long the object may be out of sight before tracking lets go. | 3 |
| Return Home After (s) | With nothing to follow for this long, go back to the home position. 0 = stay put. | 30 |
| Home Position | Blank = the camera's own home position (ONVIF). Or a saved preset. The field suggests the camera's presets. Pelco-D needs a preset number. | blank |
| Zoom While Tracking | Zoom in on a small object once it is centred, and back out near the edge or when it is lost. | Disabled |
| Target Size (%) | With zoom on: how much of the frame height the object should fill. | 30 |

How it behaves:

- **Starting.** Tracking only starts on an object that has passed this camera's
  zones and confirmation settings and has been seen on at least two detection
  cycles, so a one-frame false detection never swings the camera. When several
  match, the largest (usually the closest) is chosen.
- **Following.** It sticks with the same object (its track id, or the same
  label nearest to where it was last seen). Zones are ignored while following,
  because they stop lining up with the scene once the camera has moved.
- **Steering.** When the object is outside the dead zone, the camera moves
  towards it in a pulse. Both the speed and the length of the pulse grow with
  how far off centre the object is: a 0.3 s nudge just outside the dead zone,
  up to 1.5 s when the object is at the edge of the frame, so someone walking
  across the picture is not outrun. After each pulse the camera stops and
  waits 0.5 s, because the video arrives a little after the motor moves, and
  steering on old frames makes a camera overshoot and hunt back and forth.
- **Tilt matches the picture's shape.** The picture is wider than it is tall,
  so the same camera turn shifts the view further up/down than left/right.
  Tilt moves are scaled down to match (about 0.56x on a 16:9 camera), so a
  distant person near the top edge doesn't get the camera tilted past them.
- **Catching up.** If a sideways pulse did not gain on the object (it is still
  as far off centre, on the same side - someone walking steadily across), the
  next pulse is up to 2.5x faster and longer. Tilt is never boosted. The boost resets as soon
  as the object is centred or the camera overshoots, so someone who stops is
  not swung past.
- **Following off the edge.** An object half out of the picture often stops
  being detected. If it was last seen near the edge, the camera keeps turning
  that way for up to two pulses instead of freezing, then picks it up again
  wherever it reappears.
- **Zoom.** With zoom on, the camera zooms in only once the object has been
  centred, with no pan or tilt, for 2 seconds, so it never zooms in on someone
  just walking through the middle. It zooms out straight away if the object
  nears the edge or is too big. Each zoom step changes the lens by the same
  amount even when it rides along with a long pan move. While zoomed in, the
  same turn moves the picture further, so pan/tilt moves are halved and the
  catch-up boost is off. When the object is lost, the tracker undoes its own
  zoom-in, and return-home then restores the exact home zoom. Using the PTZ pad
  makes the tracker forget its own zoom, so it never "undoes" yours.
  Pelco-D zoom has no speed control, so on Pelco-D the tracker only zooms in
  its own short steps, never during a pan.
- **Manual control wins.** Using the PTZ pad pauses auto-tracking for 30
  seconds and drops the current target.
- **Safety.** Every pulse is short and ends with an explicit Stop (retried
  once if it fails). ONVIF moves also carry a timeout as a backup, but many
  cameras ignore it and keep moving until told to stop, so the tracker never
  relies on it. Camera commands run off the detection thread, and a camera
  whose last command is still in flight is skipped, so a slow or offline
  camera cannot stall detection.

### Tuning

Start with **Tracking Speed** 4-6 and **Zoom While Tracking** off.

- **Falls behind** (the object reaches the edge before the camera catches up):
  raise Tracking Speed.
- **Overshoots or hunts back and forth:** lower Tracking Speed, or raise
  **Dead Zone** so small drifts are ignored.
- **Twitchy while the object stands still:** raise Dead Zone (20-25%).

The pad's own **Speed** and **Step Duration** settings do not affect
tracking.

The Live page shows a line under the PTZ pad: watching for, following, paused
for manual control, or returning home.

### Alerts while tracking

While the camera moves, an object cannot be classified as moving or still
(the whole picture is moving). The existing camera-motion guard therefore only
lets an object alert during a pan when its mode on the **Objects** page is
**Moving & Still**. The first alert usually fires before tracking starts, so a
cat that walks into view still alerts. If you want alerts throughout a long
track, set that label to Moving & Still.

Tracking starts on objects that survive the moving/still filter. With the
default **Moving Only** mode, a cat sitting still will not start a track,
though one that is already being followed stays followed when it stops. Set
the label to **Moving & Still** to also start on a still object.

Behavioural detections (line crossing, loitering, unusual activity) pause
while the camera moves, as they already did for manual PTZ.
