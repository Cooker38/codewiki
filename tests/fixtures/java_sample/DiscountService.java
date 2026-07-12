package com.example.demo;

import java.util.List;
import java.util.ArrayList;

/**
 * A demo service for calculating discounts.
 */
public class DiscountService {

    private static final int MAX_DISCOUNT = 50;
    private final DiscountCalculator calculator;

    public DiscountService(DiscountCalculator calculator) {
        this.calculator = calculator;
    }

    public double calculateDiscount(int orderId, double amount) {
        Order order = new Order(orderId, amount);
        double rate = calculator.getRate(order);
        double discount = amount * rate;
        if (discount > MAX_DISCOUNT) {
            discount = MAX_DISCOUNT;
        }
        return discount;
    }

    public List<Discount> batchCalculate(List<Order> orders) {
        List<Discount> results = new ArrayList<>();
        for (Order order : orders) {
            double discount = calculateDiscount(order.getId(), order.getAmount());
            results.add(new Discount(discount));
        }
        return results;
    }
}
