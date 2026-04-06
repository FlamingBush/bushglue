#TODO fold this into readme

import time
import paho.mqtt.client as mqtt
client = mqtt.Client()
client.connect("172.26.160.1", 1883, 60)
client.publish("bush/flame/flare/pulse", 1500)
time.sleep(2)
client.publish("bush/flame/bigjet/pulse", 100)

