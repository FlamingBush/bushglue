// Emotional Fire - Spiral
//
// Pixelblaze v2/v3 1D pattern for individually addressable fixtures arranged
// along a physical spiral. Save the pattern under the name mapped by the
// bridge's set_pattern binding.
//
// The MQTT bridge stages six scores and a normalized verse hash, then changes
// sentimentTrigger. Actual bush/flame/pulse commands provide optional visual
// accents; vestigial flare and bigjet fields in sentiment are still ignored.
// Colors are calculated once per frame so Pixelblaze v2 can drive the complete
// configured pixel span.

export var inputAnger = 0
export var inputJoy = 0
export var inputLove = 0
export var inputSurprise = 0
export var inputFear = 0
export var inputSadness = 0
export var inputVerseHash = 0
export var inputSentimentVerseHash = 0
export var inputFlamePulseMs = 0
export var inputFlameValve = 0

export var sentimentTrigger = 0
export var verseTrigger = 0
export var speakingTrigger = 0
export var doneTrigger = 0
export var flamePulseTrigger = 0

// Operator-tunable inputs. All durations are seconds so they remain safely
// inside the Pixelblaze v2 16.16 number range.
export var speakingTimeoutSeconds = 30
export var anticipationTimeoutSeconds = 30
export var decaySeconds = 35
export var centerAtPixelZero = 1

// Read-only diagnostics for the Pixelblaze variable watcher:
// 0=idle, 1=anticipating, 2=speaking, 3=releasing.
export var sceneMode = 0
export var emotionLevel = 0
export var releaseWasWatchdog = 0
export var doneArmed = 0
export var lastSentimentMatched = 0
export var lastFlameValve = 0

var seenSentimentTrigger = sentimentTrigger
var seenVerseTrigger = verseTrigger
var seenSpeakingTrigger = speakingTrigger
var seenDoneTrigger = doneTrigger
var seenFlamePulseTrigger = flamePulseTrigger
var activeVerseHash = 0

var targetAnger = 0
var targetJoy = 0
var targetLove = 0
var targetSurprise = 0
var targetFear = 0
var targetSadness = 0

var anger = 0
var joy = 0
var love = 0
var surprise = 0
var fear = 0
var sadness = 0

var phaseSeconds = 0
var motionPhase = 0
var palettePhase = 0
var basePhase = 0
var flareOpenSeconds = 0
var flareAfterSeconds = 0
var bigjetOpenSeconds = 0
var bigjetAfterSeconds = 0
var poofOpenSeconds = 0
var poofAfterSeconds = 0

// Keep render() deliberately shallow. Pixelblaze v2 can preview a complex
// per-pixel render while failing to finish the corresponding physical output.
// These buffers move that work into beforeRender and always follow pixelCount.
var frameHue = array(pixelCount)
var frameSaturation = array(pixelCount)
var frameValue = array(pixelCount)

export function toggleCenterAtPixelZero(v) {
  centerAtPixelZero = v
}

function captureSentiment() {
  targetAnger = clamp(inputAnger, 0, 1)
  targetJoy = clamp(inputJoy, 0, 1)
  targetLove = clamp(inputLove, 0, 1)
  targetSurprise = clamp(inputSurprise, 0, 1)
  targetFear = clamp(inputFear, 0, 1)
  targetSadness = clamp(inputSadness, 0, 1)
  emotionLevel = 1
}

function receiveSentiment() {
  if (activeVerseHash != 0 && inputSentimentVerseHash == activeVerseHash) {
    captureSentiment()
    lastSentimentMatched = 1
  } else {
    lastSentimentMatched = 0
  }
}

function receiveFlamePulse() {
  var duration = clamp(inputFlamePulseMs, 50, 5000) / 1000
  var valve = floor(inputFlameValve + .5)
  lastFlameValve = valve
  if (valve == 1) {
    flareOpenSeconds = max(flareOpenSeconds, duration)
    flareAfterSeconds = max(flareAfterSeconds, duration + 1.2)
  } else if (valve == 2) {
    bigjetOpenSeconds = max(bigjetOpenSeconds, duration)
    bigjetAfterSeconds = max(bigjetAfterSeconds, duration + 2.5)
  } else if (valve == 3) {
    poofOpenSeconds = max(poofOpenSeconds, duration)
    poofAfterSeconds = max(poofAfterSeconds, duration + .6)
  }
}

// Every bridge trigger shares one 1..29999 event counter. Comparing modulo the
// wrap point preserves cross-topic order when several updates land in one frame.
function triggerAfter(candidate, reference) {
  var distance = candidate - reference
  if (distance <= 0) distance = distance + 29999
  return distance < 15000
}

function beginAnticipation() {
  sceneMode = 1
  phaseSeconds = 0
  releaseWasWatchdog = 0
  doneArmed = 0
  lastSentimentMatched = 0
}

function beginSpeaking() {
  sceneMode = 2
  phaseSeconds = 0
  releaseWasWatchdog = 0
  doneArmed = 1
}

function beginRelease(watchdog) {
  sceneMode = 3
  phaseSeconds = 0
  releaseWasWatchdog = watchdog
  doneArmed = 0
}

function totalScore() {
  return anger + joy + love + surprise + fear + sadness
}

function emotionTempo() {
  var total = totalScore()
  if (total < .0001) return .55
  return (anger * 1.35 + joy * .9 + love * .5 + surprise * 1.65 +
    fear * .72 + sadness * .38) / total
}

// Select an emotion spatially rather than averaging RGB values. This preserves
// saturated colors and lets all six scores remain visible without making mud.
function emotionAt(selector) {
  var total = totalScore()
  if (total < .0001) return -1
  var pick = selector * total
  if (pick < anger) return 0
  pick = pick - anger
  if (pick < joy) return 1
  pick = pick - joy
  if (pick < love) return 2
  pick = pick - love
  if (pick < surprise) return 3
  pick = pick - surprise
  if (pick < fear) return 4
  return 5
}

function primaryHue(emotion) {
  if (emotion == 0) return .76 // anger: ultraviolet
  if (emotion == 1) return .47 // joy: turquoise
  if (emotion == 2) return .84 // love: orchid
  if (emotion == 3) return .51 // surprise: icy cyan
  if (emotion == 4) return .44 // fear: green-teal
  if (emotion == 5) return .64 // sadness: cobalt
  return .69
}

function accentHue(emotion) {
  if (emotion == 0) return .97 // crimson
  if (emotion == 1) return .49 // pale aqua
  if (emotion == 2) return .94 // rose
  if (emotion == 3) return .60 // electric blue
  if (emotion == 4) return .39 // unnatural mint
  if (emotion == 5) return .70 // indigo
  return .75
}

function primarySaturation(emotion) {
  if (emotion == 1) return .76
  if (emotion == 2) return .74
  if (emotion == 3) return .42
  if (emotion == 4) return .86
  if (emotion == 5) return .82
  return .88
}

function accentSaturation(emotion) {
  if (emotion == 1) return .34
  if (emotion == 2) return .70
  if (emotion == 3) return .76
  if (emotion == 4) return .66
  if (emotion == 5) return .74
  return .90
}

function emotionalBrightness(emotion) {
  if (emotion == 1) return 1.05
  if (emotion == 2) return .90
  if (emotion == 3) return 1.15
  if (emotion == 4) return .72
  if (emotion == 5) return .62
  return 1
}

export function beforeRender(delta) {
  var seconds = delta / 1000
  phaseSeconds = min(1000, phaseSeconds + seconds)

  var verseChanged = verseTrigger != seenVerseTrigger
  var sentimentChanged = sentimentTrigger != seenSentimentTrigger
  var speakingChanged = speakingTrigger != seenSpeakingTrigger
  var doneChanged = doneTrigger != seenDoneTrigger
  var flamePulseChanged = flamePulseTrigger != seenFlamePulseTrigger

  if (verseChanged) {
    seenVerseTrigger = verseTrigger
    activeVerseHash = inputVerseHash
    beginAnticipation()
  }
  if (sentimentChanged) {
    seenSentimentTrigger = sentimentTrigger
    receiveSentiment()
  }
  if (speakingChanged) {
    seenSpeakingTrigger = speakingTrigger
    if (!verseChanged || triggerAfter(speakingTrigger, verseTrigger)) {
      beginSpeaking()
    }
  }
  if (doneChanged) {
    seenDoneTrigger = doneTrigger
    var doneAfterVerse = !verseChanged || triggerAfter(doneTrigger, verseTrigger)
    var doneAfterSpeaking = !speakingChanged ||
      triggerAfter(doneTrigger, speakingTrigger)
    if (doneArmed && doneAfterVerse && doneAfterSpeaking) beginRelease(0)
  }
  if (flamePulseChanged) {
    seenFlamePulseTrigger = flamePulseTrigger
    receiveFlamePulse()
  }

  flareOpenSeconds = max(0, flareOpenSeconds - seconds)
  flareAfterSeconds = max(0, flareAfterSeconds - seconds)
  bigjetOpenSeconds = max(0, bigjetOpenSeconds - seconds)
  bigjetAfterSeconds = max(0, bigjetAfterSeconds - seconds)
  poofOpenSeconds = max(0, poofOpenSeconds - seconds)
  poofAfterSeconds = max(0, poofAfterSeconds - seconds)

  var anticipationLimit = clamp(anticipationTimeoutSeconds, 5, 180)
  var speakingLimit = clamp(speakingTimeoutSeconds, 5, 300)
  var releaseDuration = clamp(decaySeconds, 5, 180)
  if (sceneMode == 1 && phaseSeconds >= anticipationLimit) beginRelease(1)
  if (sceneMode == 2 && phaseSeconds >= speakingLimit) beginRelease(1)

  if (sceneMode == 3) {
    emotionLevel = max(0, emotionLevel - seconds / releaseDuration)
    if (phaseSeconds >= releaseDuration) sceneMode = 0
  } else if (sceneMode == 0) {
    emotionLevel = max(0, emotionLevel - seconds / (releaseDuration * 2))
  }

  var blend = min(1, seconds * 2)
  anger = anger + (targetAnger - anger) * blend
  joy = joy + (targetJoy - joy) * blend
  love = love + (targetLove - love) * blend
  surprise = surprise + (targetSurprise - surprise) * blend
  fear = fear + (targetFear - fear) * blend
  sadness = sadness + (targetSadness - sadness) * blend

  var tempo = emotionTempo()
  motionPhase = frac(motionPhase + seconds * (.035 + .06 * tempo))
  palettePhase = frac(palettePhase + seconds / 90)
  basePhase = frac(basePhase + seconds / 14)

  for (var frameIndex = 0; frameIndex < pixelCount; frameIndex++) {
    calculatePixel(frameIndex)
  }
}

function calculatePixel(index) {
  var radius = index / max(1, pixelCount - 1)
  if (centerAtPixelZero < .5) radius = 1 - radius

  var selector = frac(radius * .73 + palettePhase)
  var emotion = emotionAt(selector)
  var movement = .12 + .10 * wave(basePhase - radius * .37)
  var edge = 0

  if (sceneMode == 1) {
    // Repeating outer-to-center gathering breath.
    var inwardHead = 1 - frac(phaseSeconds / 4)
    var inwardBand = clamp(1 - abs(radius - inwardHead) * 5, 0, 1)
    movement = .18 + inwardBand * .82
    edge = inwardBand
  } else if (sceneMode == 2) {
    // Speech pushes broad waves from the center toward the perimeter.
    movement = .25 + .75 * wave(motionPhase - radius * .42)
    edge = wave(motionPhase * 1.7 - radius * 1.8)
  } else if (sceneMode == 3) {
    // A single four-second center-out exhale leads the long color decay.
    var releaseProgress = clamp(phaseSeconds / 4, 0, 1)
    var outwardBand = clamp(1 - abs(radius - releaseProgress) * 4, 0, 1)
    movement = .12 + outwardBand * .88
    edge = outwardBand
  }

  var targetHue = primaryHue(emotion)
  var targetSaturation = primarySaturation(emotion)
  var accentAmount = clamp((edge - .68) / .32, 0, 1)
  targetHue = targetHue + (accentHue(emotion) - targetHue) * accentAmount
  targetSaturation = targetSaturation +
    (accentSaturation(emotion) - targetSaturation) * accentAmount

  // Midnight indigo is always present; emotional colors emerge from it.
  var hue = .69 + (targetHue - .69) * emotionLevel
  var saturation = .78 + (targetSaturation - .78) * emotionLevel
  var smoke = wave(basePhase + radius * 1.3)
  var emberTwinkle = wave(basePhase * 3 + radius * 2.1)
  var value = .035 + .045 * smoke + .04 * emberTwinkle * emberTwinkle
  value = value + emotionLevel * (.055 + .24 * movement) *
    emotionalBrightness(emotion)

  // Actual valve commands briefly yield visual priority to the physical fire.
  // Their independent timers mirror concurrent valves and extend-only pulses.
  var flareOpen = clamp(flareOpenSeconds * 20, 0, 1)
  var bigjetOpen = clamp(bigjetOpenSeconds * 10, 0, 1)
  var poofOpen = clamp(poofOpenSeconds * 20, 0, 1)
  var flareAfter = clamp(flareAfterSeconds / 1.2, 0, 1) * (1 - flareOpen)
  var bigjetAfter = clamp(bigjetAfterSeconds / 2.5, 0, 1) * (1 - bigjetOpen)
  var poofAfter = clamp(poofAfterSeconds / .6, 0, 1) * (1 - poofOpen)
  var flameDuck = max(flareOpen * .32, bigjetOpen * .68)
  flameDuck = max(flameDuck, poofOpen * .20)
  var coolStrength = max(flareAfter * .24, bigjetAfter * .52)
  coolStrength = max(coolStrength, poofAfter * .20)
  var coolHue = .52
  if (bigjetAfter > flareAfter && bigjetAfter > poofAfter) coolHue = .59
  var coolMotion = .35 + .65 * wave(radius * 1.4 +
    flareAfterSeconds * .7 + bigjetAfterSeconds * .35)
  hue = hue + (coolHue - hue) * coolStrength
  saturation = saturation + (.68 - saturation) * coolStrength
  value = value * (1 - flameDuck) +
    coolStrength * (.04 + .16 * coolMotion)
  frameHue[index] = hue
  frameSaturation[index] = saturation
  frameValue[index] = clamp(value, 0, .42)
}

export function render(index) {
  hsv(frameHue[index], frameSaturation[index], frameValue[index])
}
