package com.example.demo;

/**
 * Interface for discount calculation strategies.
 */
public interface DiscountCalculator {

    double getRate(Order order);

    double applyDiscount(double amount, double rate);
}
