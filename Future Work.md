# Future Work



## 1. Per sensor cumulatives

Each sensor should implement the following meters:

​	1.1 Daily power meter - this meter will aggregate power during the day and zero at midnight. Need to take care of the potential time differences between readings. Should use HA trapezoid calculations not to invent the wheel. Important is not to take simple averages as time between readings may vary. This meter should be in W.

​	1.2 Total power generated - always growing meter that will show total power production over lifetime of the sensor in kWh

## 2. Total cumulatives

Integration should create entities to show daily and total power meters in kWh for:

​	2.1 String

​	2.2 Total installation

## 3. Performance

​	3.1 Panel configuration should be extended to add peak panel power. Based on that there should be entity in each sensor to show performance in % which will represent 'Power'/'Peak Power'.

​	3.1. the 3.1 concept should be extended to per string and total